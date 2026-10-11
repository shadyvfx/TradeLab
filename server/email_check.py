"""Email checker: can this address actually receive email?

Standard library only. Checks run cheapest first:

1. Typos of popular providers ("gnail.com", "gmial.com", "hotmial.com") are
   rejected with a suggestion. Typo-squatters register these domains and give
   them mail servers, so a DNS lookup alone would accept them.
2. Reserved domains (example.com, .test, .invalid) and domain endings that
   don't exist (".con" for ".com") are rejected without a network lookup.
3. A DNS lookup asks for the domain's mail servers (MX records). Domains that
   don't exist, have no mail servers, or publish a "null MX" (RFC 7505: this
   domain never accepts mail) are rejected.

If DNS can't be reached (offline, or blocked by the network), the address is
accepted as "unverified" so sign-ups don't break. The same goes for networks
whose DNS claims that every domain is missing (some filters and captive
portals do): before rejecting an address, the checker confirms that DNS still
finds gmail.com's mail servers.

This proves the domain can receive email, not that the mailbox exists or
belongs to the person signing up. That needs a confirmation email.

Try it from the project folder:  python3 server/email_check.py someone@gmail.com
DNS servers come from /etc/resolv.conf, then 1.1.1.1 and 8.8.8.8. Set
TRADELAB_DNS_SERVER (for example "1.1.1.1" or "127.0.0.1:5353") to override.
"""

from __future__ import annotations

import os
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Results


@dataclass(frozen=True)
class EmailCheck:
    status: str  # "ok", "invalid" or "unverified"
    message: str
    suggestion: str | None = None
    domain: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "invalid"

    def to_dict(self) -> dict:
        return {"status": self.status, "ok": self.ok, "message": self.message, "suggestion": self.suggestion, "domain": self.domain}


# ---------------------------------------------------------------------------
# Typos and reserved domains

# Providers whose typos are common enough to catch. Each is long enough that a
# one- or two-letter slip is very unlikely to be someone's real domain.
TYPO_TARGETS = (
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "protonmail.com",
    "comcast.net",
    "verizon.net",
    "sbcglobal.net",
)

# Real mail domains that are close to a typo target and must never be "corrected".
# The last row holds companies one letter away from a provider (cloud.com is
# Citrix's parent, grail.com a medical company, zmail.com belongs to Zoom).
KNOWN_DOMAINS = frozenset(
    TYPO_TARGETS
    + (
        "mail.com", "email.com", "gmx.com", "gmx.net", "ymail.com", "rocketmail.com", "live.com", "msn.com",
        "aol.com", "aim.com", "me.com", "mac.com", "proton.me", "pm.me", "zoho.com", "yandex.com", "hey.com",
        "fastmail.com", "tutanota.com", "hotmail.co.uk", "yahoo.co.uk", "outlook.co.uk", "hotmail.fr",
        "yahoo.fr", "outlook.fr", "hotmail.de", "outlook.de", "yahoo.de", "verizon.com", "comcast.com",
        "cloud.com", "pcloud.com", "grail.com", "zmail.com", "qmail.com",
    )
)

# Endings that look like a slip of the finger and aren't real top-level domains.
TLD_FIXES = {
    "con": "com", "cmo": "com", "ocm": "com", "vom": "com", "xom": "com", "cpm": "com", "comm": "com",
    "coom": "com", "cim": "com", "nte": "net", "ner": "net", "nett": "net", "ogr": "org", "orgg": "org",
    "rog": "org", "eud": "edu", "ed": "edu",
}

# Reserved for documentation and testing (RFC 2606, RFC 6761); they never receive mail.
RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
RESERVED_TLDS = ("test", "example", "invalid", "localhost", "local")


def _distance(a: str, b: str) -> int:
    """Edits to turn a into b: insert, delete, replace or swap two neighbors."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(row[j - 1] + 1, rows[i - 1][j] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[i - 2][j - 2] + 1)
        rows.append(row)
    return rows[-1][-1]


def typo_suggestion(domain: str) -> str | None:
    """The provider this domain is probably a typo of, or None.

    One slip (a letter missing, extra, wrong or swapped) counts anywhere. Longer
    domains may have two, but only when the ending matches: hotmail.ca and
    outlook.cl are real Microsoft domains two edits from hotmail.com and
    outlook.com, not typos.
    """
    domain = domain.lower()
    if domain in KNOWN_DOMAINS:
        return None
    ending = domain.rpartition(".")[2]
    best = None
    for target in TYPO_TARGETS:
        limit = 2 if len(domain) >= 10 and ending == target.rpartition(".")[2] else 1
        distance = _distance(domain, target)
        if 0 < distance <= limit and (best is None or distance < best[0]):
            best = (distance, target)
    return best[1] if best else None


def _is_reserved(domain: str) -> bool:
    domain = domain.lower()
    return any(domain == item or domain.endswith("." + item) for item in RESERVED_DOMAINS) or domain.rsplit(".", 1)[-1] in RESERVED_TLDS


# ---------------------------------------------------------------------------
# A small DNS client for MX lookups (RFC 1035 wire format over UDP, with TCP
# when a reply is truncated)

TYPE_MX = 15
TYPE_OPT = 41


def _encode_name(domain: str) -> bytes:
    out = b""
    for label in domain.strip(".").split("."):
        raw = label.encode("idna")
        if not 0 < len(raw) < 64:
            raise ValueError("bad label")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def build_query(domain: str, query_id: int, qtype: int = TYPE_MX) -> bytes:
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 1)  # recursion desired; one OPT record
    question = _encode_name(domain) + struct.pack("!HH", qtype, 1)
    edns = b"\0" + struct.pack("!HHIH", TYPE_OPT, 1232, 0, 0)  # accept replies up to 1232 bytes
    return header + question + edns


def _read_name(message: bytes, offset: int) -> tuple[str, int]:
    labels, end, jumps = [], None, 0
    while True:
        if offset >= len(message):
            raise ValueError("truncated name")
        length = message[offset]
        if length & 0xC0 == 0xC0:  # compression pointer
            if offset + 1 >= len(message):
                raise ValueError("truncated pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | message[offset + 1]
            jumps += 1
            if jumps > 64:
                raise ValueError("pointer loop")
            continue
        if length & 0xC0:
            raise ValueError("bad label type")
        offset += 1
        if length == 0:
            break
        labels.append(message[offset : offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels), end if end is not None else offset


def parse_mx_response(message: bytes, query_id: int) -> tuple[int, bool, list[tuple[int, str]]]:
    """Returns (rcode, truncated, [(preference, exchange)]). The root exchange "." comes back as ""."""
    if len(message) < 12:
        raise ValueError("short reply")
    reply_id, flags, qdcount, ancount, _nscount, _arcount = struct.unpack("!HHHHHH", message[:12])
    if reply_id != query_id or not flags & 0x8000:
        raise ValueError("not our reply")
    offset = 12
    for _ in range(qdcount):
        _, offset = _read_name(message, offset)
        offset += 4
    records = []
    for _ in range(ancount):
        _, offset = _read_name(message, offset)
        if offset + 10 > len(message):
            raise ValueError("truncated record")
        rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", message[offset : offset + 10])
        offset += 10
        if rtype == TYPE_MX and rdlength >= 3:
            preference = struct.unpack("!H", message[offset : offset + 2])[0]
            exchange, _ = _read_name(message, offset + 2)
            records.append((preference, exchange))
        offset += rdlength
    return flags & 0x000F, bool(flags & 0x0200), records


def _split_server(server: str) -> tuple[str, int]:
    if server.startswith("["):  # [IPv6]:port
        host, _, port = server[1:].partition("]")
        return host, int(port.lstrip(":") or 53)
    if server.count(":") == 1:  # IPv4:port
        host, port = server.split(":")
        return host, int(port)
    return server, 53


def _exchange(server: str, payload: bytes, timeout: float, tcp: bool) -> bytes:
    host, port = _split_server(server)
    kind = socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
    family, _, _, _, address = socket.getaddrinfo(host, port, 0, kind)[0]
    with socket.socket(family, kind) as sock:
        sock.settimeout(timeout)
        if tcp:
            sock.connect(address)
            sock.sendall(struct.pack("!H", len(payload)) + payload)
            size = struct.unpack("!H", _recv_exactly(sock, 2))[0]
            return _recv_exactly(sock, size)
        sock.sendto(payload, address)
        deadline = time.monotonic() + timeout
        while True:
            data, _ = sock.recvfrom(4096)
            if len(data) >= 2 and data[:2] == payload[:2]:
                return data
            if time.monotonic() > deadline:
                raise socket.timeout("no matching reply")


def _recv_exactly(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ValueError("connection closed")
        data += chunk
    return data


def lookup_mx(domain: str, nameservers: list[str], timeout: float = 1.5, budget: float = 4.0) -> tuple[str, list[tuple[int, str]]]:
    """Asks each nameserver in turn. Returns ("ok", records), ("nxdomain", []) or ("unknown", [])."""
    started = time.monotonic()
    for server in nameservers:
        if time.monotonic() - started > budget:
            break
        query_id = secrets.randbelow(65536)
        payload = build_query(domain, query_id)
        try:
            rcode, truncated, records = parse_mx_response(_exchange(server, payload, timeout, tcp=False), query_id)
            if truncated and not records:
                rcode, _, records = parse_mx_response(_exchange(server, payload, timeout, tcp=True), query_id)
        except (OSError, ValueError, UnicodeError, IndexError, struct.error):
            continue  # unreachable, timed out or garbled: try the next server
        if rcode == 0:
            return "ok", records
        if rcode == 3:
            return "nxdomain", []
        # SERVFAIL, REFUSED and the like: ask the next server
    return "unknown", []


def system_nameservers() -> list[str]:
    configured = os.environ.get("TRADELAB_DNS_SERVER", "").strip()
    if configured:
        return [server.strip() for server in configured.split(",") if server.strip()]
    servers = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver" and parts[1] not in servers:
                    servers.append(parts[1])
    except OSError:
        pass
    return servers[:3] + [server for server in ("1.1.1.1", "8.8.8.8") if server not in servers]


# ---------------------------------------------------------------------------
# The checker


# A domain that always has mail servers. If DNS says it doesn't, DNS is lying.
CANARY_DOMAIN = "gmail.com"


class EmailChecker:
    """Call with a well-formed address (lowercase is fine); returns an EmailCheck.

    Lookups are cached for ten minutes, so checking while someone types is cheap.
    Pass `lookup` (a function: domain -> (result, records)) to test without a network.
    """

    def __init__(self, nameservers: list[str] | None = None, lookup=None, cache_seconds: int = 600):
        self.nameservers = nameservers if nameservers is not None else system_nameservers()
        self.lookup = lookup or (lambda domain: lookup_mx(domain, self.nameservers))
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, tuple[str, list]]] = {}
        self._lock = threading.Lock()

    def _lookup(self, domain: str) -> tuple[str, list]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(domain)
            if cached and now - cached[0] < self.cache_seconds:
                return cached[1]
        result = self.lookup(domain)
        if result[0] != "unknown":
            with self._lock:
                if len(self._cache) > 1000:
                    self._cache.clear()
                self._cache[domain] = (now, result)
        return result

    def __call__(self, email: str) -> EmailCheck:
        local, _, domain = email.strip().lower().rpartition("@")

        typo = typo_suggestion(domain)
        if typo:
            return EmailCheck("invalid", f"Did you mean {local}@{typo}? {domain} looks like a typo.", f"{local}@{typo}", domain)

        if _is_reserved(domain):
            return EmailCheck("invalid", f"{domain} is reserved for examples and testing, so it can't receive email. Use your real address.", None, domain)

        name, _, tld = domain.rpartition(".")
        if tld in TLD_FIXES:
            fixed = f"{name}.{TLD_FIXES[tld]}"
            fixed = typo_suggestion(fixed) or fixed  # gmaill.con -> gmail.com
            return EmailCheck("invalid", f"Did you mean {local}@{fixed}? Email addresses don't end in .{tld}.", f"{local}@{fixed}", domain)

        result, records = self._lookup(domain)
        if result == "ok" and _has_mail_servers(records):
            return EmailCheck("ok", f"{domain} can receive email.", None, domain)
        if result == "unknown":
            return EmailCheck("unverified", "Couldn't reach a DNS server to check this address, so it wasn't verified.", None, domain)
        if not self._dns_is_trustworthy():
            return EmailCheck("unverified", "This network's DNS couldn't check this address, so it wasn't verified.", None, domain)
        if result == "nxdomain":
            return EmailCheck("invalid", f"{domain} doesn't exist. Check the spelling of your email address.", None, domain)
        if not records:
            return EmailCheck("invalid", f"{domain} doesn't receive email. Check the spelling, or use a different address.", None, domain)
        return EmailCheck("invalid", f"{domain} doesn't accept email. Use a different address.", None, domain)

    def _dns_is_trustworthy(self) -> bool:
        """Whether DNS finds mail servers for a domain that certainly has them."""
        result, records = self._lookup(CANARY_DOMAIN)
        return result == "ok" and _has_mail_servers(records)


def _has_mail_servers(records) -> bool:
    """At least one real MX record (a null MX, "." or "", means the domain refuses mail)."""
    return any(exchange not in ("", ".") for _, exchange in records)


if __name__ == "__main__":
    import sys

    checker = EmailChecker()
    print("DNS servers:", ", ".join(checker.nameservers))
    for address in sys.argv[1:] or ["someone@gmail.com", "someone@gnail.com", "someone@no-such-domain-4815.com"]:
        result = checker(address)
        print(f"{address}: {result.status} - {result.message}")
