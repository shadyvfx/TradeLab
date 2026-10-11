#!/usr/bin/env python3
"""TradeLab's production server, over HTTPS or HTTP.

One process runs everything TradeLab needs: the website, sign-up and sign-in,
each account's saved progress, the email checker and the AI tutor, including
its streamed answers. It runs on Hypercorn, a production server for macOS,
Linux and Windows that handles HTTPS itself (HTTP/2 included) and never lets
a slow or idle connection hold up other visitors.

    python3 serve.py
        http://127.0.0.1:5001, for this computer only.

    python3 serve.py --https --host 0.0.0.0
        https://<this computer>:8443, for phones and laptops on your network.
        Makes its own certificate on the first run. Browsers warn about it
        until you trust TradeLab's local certificate authority on that device;
        docs/deployment.md shows how.

    sudo python3 serve.py --https --host 0.0.0.0 --port 443 --redirect-port 80 \\
        --cert /etc/letsencrypt/live/example.com/fullchain.pem \\
        --key /etc/letsencrypt/live/example.com/privkey.pem
        A real certificate (Let's Encrypt) on a server with a domain name.

    python3 serve.py --behind-proxy
        Behind Caddy, nginx or a hosting platform that handles HTTPS.

Run python3 serve.py --help for every option. Each option can also be set
with an environment variable, which hosting platforms use; PORT is honored.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import ipaddress
import logging
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_HTTP_PORT = 5001  # macOS uses 5000 for AirPlay
DEFAULT_HTTPS_PORT = 8443
DEFAULT_THREADS = 32  # requests at once; a streamed tutor answer holds one for up to two minutes
DEFAULT_CERT_DIR = Path.home() / ".tradelab" / "certs"
AUTHORITY_DAYS = 10 * 365  # TradeLab's local certificate authority
LEAF_DAYS = 365  # each server certificate it signs
RENEW_WITHIN_DAYS = 30
# Larger than the biggest request the app accepts (saved progress, about 2 MB),
# so the app answers those with its own message; bigger bodies are refused here.
MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
MAX_REQUEST_HEADER_BYTES = 64 * 1024

log = logging.getLogger("tradelab")
access_log = logging.getLogger("tradelab.access")


class SetupError(Exception):
    """A problem with the options or the computer, explained for the person starting the server."""


# -- Settings ----------------------------------------------------------------


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = DEFAULT_HTTP_PORT
    https: bool = False
    cert: Path | None = None
    key: Path | None = None
    local_certificate: bool = False  # make (or reuse) a certificate for this computer
    cert_dir: Path = DEFAULT_CERT_DIR
    redirect_port: int | None = None
    proxy_hops: int = 0
    threads: int = DEFAULT_THREADS
    db: str | None = None
    model_url: str | None = None
    access_log: bool = True
    extra_names: list[str] = field(default_factory=list)


def _env_flag(environ, name: str) -> bool:
    return (environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(environ, name: str) -> int | None:
    value = (environ.get(name) or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise SetupError(f"{name} must be a whole number, not {value!r}.") from None


def chat_completions_url(value: str) -> str:
    """llama-server's chat URL from an address like 192.168.1.23:8080 or http://host:8080."""
    value = value.strip().rstrip("/")
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "http://" + value
    if urlsplit(value).path in {"", "/"}:
        value += "/v1/chat/completions"
    return value


def parse_settings(argv=None, environ=None) -> Settings:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        prog="serve.py",
        description="Run TradeLab on a production web server, over HTTPS or HTTP.",
        epilog="docs/deployment.md has step-by-step setups for a laptop, a home network and a server with a domain.",
    )
    parser.add_argument("--host", help="address to listen on (default 127.0.0.1, this computer only; 0.0.0.0 for every network)")
    parser.add_argument("--port", type=int, help=f"port (default {DEFAULT_HTTP_PORT}, or {DEFAULT_HTTPS_PORT} with --https; or $PORT)")
    parser.add_argument("--https", action="store_true", help="serve over HTTPS (with a certificate made for this computer, unless you pass --cert and --key)")
    parser.add_argument("--cert", help="certificate file (PEM), including any intermediate certificates")
    parser.add_argument("--key", help="private key file (PEM) for --cert")
    parser.add_argument("--cert-name", action="append", default=[], metavar="NAME",
                        help="another hostname or IP address for the certificate made for this computer (repeatable)")
    parser.add_argument("--redirect-port", type=int, help="also listen for plain HTTP on this port and redirect it to HTTPS (for example 80)")
    parser.add_argument("--behind-proxy", type=int, nargs="?", const=1, metavar="HOPS",
                        help="trust X-Forwarded-* headers from this many reverse proxies (default 1 when given)")
    parser.add_argument("--threads", type=int, help=f"worker threads (default {DEFAULT_THREADS})")
    parser.add_argument("--db", help="the user database file (default tradelab.db in this folder)")
    parser.add_argument("--model-url", help="llama-server's address, for example http://192.168.1.23:8080")
    parser.add_argument("--no-access-log", action="store_true", help="don't print a line for each request")
    args = parser.parse_args(argv)

    settings = Settings()
    settings.https = args.https or _env_flag(environ, "TRADELAB_HTTPS") or bool(args.cert or environ.get("TRADELAB_TLS_CERT"))
    settings.host = args.host or environ.get("TRADELAB_HOST") or "127.0.0.1"
    port = args.port if args.port is not None else _env_int(environ, "PORT")
    if port is None:
        port = _env_int(environ, "TRADELAB_PORT")
    settings.port = port if port is not None else (DEFAULT_HTTPS_PORT if settings.https else DEFAULT_HTTP_PORT)

    cert = args.cert or environ.get("TRADELAB_TLS_CERT")
    key = args.key or environ.get("TRADELAB_TLS_KEY")
    if bool(cert) != bool(key):
        raise SetupError("Give both --cert and --key (the certificate and its private key), or neither.")
    if cert:
        settings.cert, settings.key = Path(cert).expanduser(), Path(key).expanduser()
    settings.local_certificate = settings.https and not cert
    settings.cert_dir = Path(environ.get("TRADELAB_CERT_DIR") or DEFAULT_CERT_DIR).expanduser()
    settings.extra_names = list(args.cert_name)

    redirect = args.redirect_port if args.redirect_port is not None else _env_int(environ, "TRADELAB_REDIRECT_PORT")
    if redirect is not None and not settings.https:
        raise SetupError("--redirect-port sends visitors to HTTPS, so it needs --https too.")
    if redirect is not None and redirect == settings.port:
        raise SetupError("--redirect-port must be different from --port.")
    settings.redirect_port = redirect

    hops = args.behind_proxy if args.behind_proxy is not None else _env_int(environ, "TRADELAB_PROXY_HOPS")
    settings.proxy_hops = max(0, hops or 0)
    threads = args.threads if args.threads is not None else _env_int(environ, "TRADELAB_THREADS")
    settings.threads = max(4, threads or DEFAULT_THREADS)
    settings.db = args.db or environ.get("TRADELAB_DB") or None
    model = args.model_url or environ.get("TRADELAB_LLAMA_URL")
    settings.model_url = chat_completions_url(model) if model else None
    settings.access_log = not (args.no_access_log or _env_flag(environ, "TRADELAB_NO_ACCESS_LOG"))

    for name, port_value in (("--port", settings.port), ("--redirect-port", settings.redirect_port)):
        if port_value is not None and not 0 < port_value < 65536:
            raise SetupError(f"{name} must be between 1 and 65535.")
    return settings


# -- Certificates ------------------------------------------------------------

HOSTNAME_LABELS = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def lan_addresses() -> list[str]:
    """This computer's IPv4 addresses on its networks (no packets are sent to find them)."""
    found = []
    for probe in ("192.0.2.1", "10.255.255.255", "172.31.255.255"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.connect((probe, 9))
                address = udp.getsockname()[0]
        except OSError:
            continue
        if address not in found and not address.startswith(("127.", "0.")):
            found.append(address)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found and not address.startswith(("127.", "0.")):
                found.append(address)
    except OSError:
        pass
    return found


def certificate_names(host: str, extra: list[str] | None = None) -> tuple[list[str], list[str]]:
    """The hostnames and IP addresses this computer's certificate should cover."""
    names, addresses = ["localhost"], ["127.0.0.1", "::1"]
    hostname = socket.gethostname().strip().lower().rstrip(".")
    for candidate in (hostname, hostname.split(".")[0], hostname.split(".")[0] + ".local"):
        if candidate and HOSTNAME_LABELS.match(candidate) and candidate not in names:
            names.append(candidate)
    if host in {"", "0.0.0.0", "::"}:
        addresses += [address for address in lan_addresses() if address not in addresses]
    else:
        try:
            if not ipaddress.ip_address(host).is_unspecified and host not in addresses:
                addresses.append(host)
        except ValueError:
            if host.lower() not in names:
                names.append(host.lower())
    for item in extra or []:
        item = item.strip().lower()
        try:
            address = str(ipaddress.ip_address(item))
        except ValueError:
            if item and item not in names:
                names.append(item)
        else:
            if address not in addresses:
                addresses.append(address)
    return names, addresses


def _need_cryptography():
    if importlib.util.find_spec("cryptography") is None:
        raise SetupError(
            "Making a certificate for HTTPS needs the cryptography package. Install the server's packages with "
            "'python3 -m pip install -r requirements.txt', or pass your own certificate with --cert and --key."
        ) from None


@dataclass
class LocalCertificate:
    cert: Path  # the server's certificate
    key: Path  # its private key (readable by you only)
    authority: Path  # the local certificate authority that signed it: trust this on your devices
    made: bool  # whether the server certificate was made just now


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _key_usage(**allowed):
    from cryptography import x509

    names = (
        "digital_signature", "content_commitment", "key_encipherment", "data_encipherment",
        "key_agreement", "key_cert_sign", "crl_sign", "encipher_only", "decipher_only",
    )
    return x509.KeyUsage(**{name: allowed.get(name, False) for name in names})


def _local_authority(directory: Path):
    """TradeLab's local certificate authority: made once, then reused, like mkcert's."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_path = directory / "tradelab-local-ca.pem"
    key_path = directory / "tradelab-local-ca-key.pem"
    if cert_path.is_file() and key_path.is_file():
        try:
            authority = x509.load_pem_x509_certificate(cert_path.read_bytes())
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            fits = authority.public_key().public_numbers() == key.public_key().public_numbers()
            fresh = authority.not_valid_after_utc - datetime.now(timezone.utc) > timedelta(days=LEAF_DAYS)
            if fits and fresh:
                return authority, key, cert_path, False
        except Exception:  # unreadable: make a new authority
            pass

    key = ec.generate_private_key(ec.SECP256R1())
    hostname = socket.gethostname().split(".")[0][:30] or "this computer"
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TradeLab"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"TradeLab local CA ({hostname})"),
        ]
    )
    now = datetime.now(timezone.utc)
    authority = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=AUTHORITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(digital_signature=True, key_cert_sign=True, crl_sign=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(
        key_path,
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )
    cert_path.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    return authority, key, cert_path, True


def _certificate_covers(cert_path: Path, key_path: Path, authority, names: list[str], addresses: list[str]) -> bool:
    if not (cert_path.is_file() and key_path.is_file()):
        return False
    from cryptography import x509

    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        certificate.verify_directly_issued_by(authority)
        sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception:  # unreadable, signed by another authority, or missing its names: make a new one
        return False
    if certificate.not_valid_after_utc - datetime.now(timezone.utc) < timedelta(days=RENEW_WITHIN_DAYS):
        return False
    covered_names = {name.lower() for name in sans.get_values_for_type(x509.DNSName)}
    covered_addresses = {str(address) for address in sans.get_values_for_type(x509.IPAddress)}
    return set(names) <= covered_names and {str(ipaddress.ip_address(a)) for a in addresses} <= covered_addresses


def ensure_local_certificate(directory: Path, names: list[str], addresses: list[str]) -> LocalCertificate:
    """A certificate for this computer's names and addresses, signed by TradeLab's local authority.

    The certificate is reused while it still covers every name and isn't about
    to expire, and made anew otherwise (for example after joining a new
    network). Trusting the authority once on a device removes the browser's
    warning there for good. Private keys are written readable by you only.
    """
    _need_cryptography()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    authority, authority_key, authority_path, new_authority = _local_authority(directory)
    cert_path = directory / "tradelab-server.pem"
    key_path = directory / "tradelab-server-key.pem"
    if not new_authority and _certificate_covers(cert_path, key_path, authority, names, addresses):
        return LocalCertificate(cert_path, key_path, authority_path, False)

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TradeLab"),
                    x509.NameAttribute(NameOID.COMMON_NAME, (names or ["localhost"])[0][:64]),
                ]
            )
        )
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(min(now + timedelta(days=LEAF_DAYS), authority.not_valid_after_utc))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(name) for name in names]
                + [x509.IPAddress(ipaddress.ip_address(address)) for address in addresses]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(digital_signature=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key()), critical=False)
        .sign(authority_key, hashes.SHA256())
    )
    _write_private(
        key_path,
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return LocalCertificate(cert_path, key_path, authority_path, True)


def check_certificate(cert: Path, key: Path) -> None:
    """Stops with a clear message if the certificate and key can't be used for HTTPS."""
    for path, what in ((cert, "certificate"), (key, "private key")):
        if not path.is_file():
            raise SetupError(f"The {what} file {path} doesn't exist.")
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert), str(key))
    except (OSError, ssl.SSLError) as error:
        raise SetupError(f"Couldn't use {cert} and {key} for HTTPS: {error}") from None


# -- WSGI layers -------------------------------------------------------------


class _LoggedBody:
    """Passes a response through chunk by chunk (so streams stay streams) and logs it at the end."""

    def __init__(self, body, finish):
        self._body = body
        self._finish = finish
        self._bytes = 0

    def __iter__(self):
        for chunk in self._body:
            self._bytes += len(chunk)
            yield chunk

    def close(self):
        try:
            close = getattr(self._body, "close", None)
            if close is not None:
                close()
        finally:
            self._finish(self._bytes)


class AccessLog:
    """One line per request: client, method, path (never the query or body), status, size and time."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        started = time.perf_counter()
        status = {"code": "-"}

        def capture(status_line, headers, exc_info=None):
            status["code"] = status_line.split(" ", 1)[0]
            return start_response(status_line, headers, exc_info)

        def finish(size):
            access_log.info(
                '%s "%s %s" %s %d %.0fms',
                environ.get("REMOTE_ADDR", "-"),
                environ.get("REQUEST_METHOD", "-"),
                environ.get("PATH_INFO", "") or "/",
                status["code"],
                size,
                (time.perf_counter() - started) * 1000,
            )

        return _LoggedBody(self.app(environ, capture), finish)


def redirect_to_https(https_port: int):
    """A WSGI app that sends every plain-HTTP request to the same address on HTTPS."""

    def app(environ, start_response):
        host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "localhost"
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 or (host.startswith("[") and "]:" in host) else host
        if not re.match(r"^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)$", hostname):
            hostname = environ.get("SERVER_NAME") or "localhost"
        target = environ.get("REQUEST_URI") or ""
        if not target.startswith("/"):
            # WSGI gives the path as UTF-8 bytes in a latin-1 string; turn it back into a URL.
            path = (environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "")) or "/"
            path = path.encode("latin-1").decode("utf-8", "replace")
            target = quote(path, safe="/;=,:@!$&'()*+~-._")
            if environ.get("QUERY_STRING"):
                target += "?" + environ["QUERY_STRING"]
        port = "" if https_port == 443 else f":{https_port}"
        location = f"https://{hostname}{port}{target}"
        method = environ.get("REQUEST_METHOD", "GET")
        status = "301 Moved Permanently" if method in {"GET", "HEAD"} else "308 Permanent Redirect"
        body = f"TradeLab uses HTTPS: {location}\n".encode("utf-8")
        start_response(
            status,
            [
                ("Location", location),
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [] if method == "HEAD" else [body]

    return app


class _BodyThatStarts:
    """Yields one empty chunk when a response has no body at all.

    Hypercorn sends a WSGI response's status line along with its first body
    chunk, so a response with no chunks (204 No Content, 304 Not Modified, any
    HEAD request) would otherwise turn into a 500 error.
    """

    def __init__(self, body):
        self._body = body

    def __iter__(self):
        empty = True
        for chunk in self._body:
            empty = False
            yield chunk
        if empty:
            yield b""

    def close(self):
        close = getattr(self._body, "close", None)
        if close is not None:
            close()


def for_hypercorn(wsgi_app):
    """Wraps a WSGI app so every response starts under Hypercorn, even an empty one."""

    def app(environ, start_response):
        return _BodyThatStarts(wsgi_app(environ, start_response))

    return app


def build_application(flask_app, proxy_hops: int = 0, log_requests: bool = True):
    """The full WSGI stack: access log, then (behind a proxy) X-Forwarded-* support, then TradeLab."""
    from backend.security import trust_proxy

    application = trust_proxy(flask_app, proxy_hops)
    return AccessLog(application) if log_requests else application


# -- Serving -----------------------------------------------------------------


@dataclass
class Listener:
    """One address TradeLab listens on, and the WSGI app that answers there."""

    app: Any
    host: str
    port: int
    cert: Path | None = None  # with a key: HTTPS
    key: Path | None = None
    name: str = "TradeLab"


def hypercorn_config(listener: Listener):
    """Hypercorn settings for one listener. TLS 1.2 or newer, HTTP/2 and HTTP/1.1."""
    from hypercorn.config import Config

    config = Config()
    host = f"[{listener.host}]" if ":" in listener.host else listener.host
    config.bind = [f"{host}:{listener.port}"]
    if listener.cert is not None:
        config.certfile, config.keyfile = str(listener.cert), str(listener.key)
    config.accesslog = None  # AccessLog writes these, without query strings
    config.errorlog = logging.getLogger("hypercorn.error")
    config.include_server_header = False
    config.keep_alive_timeout = 30  # seconds an idle connection stays open
    config.ssl_handshake_timeout = 10
    config.graceful_timeout = 5  # seconds requests get to finish when stopping
    config.wsgi_max_body_size = MAX_REQUEST_BODY_BYTES
    config.h11_max_incomplete_size = MAX_REQUEST_HEADER_BYTES
    return config


def check_port_free(host: str, port: int, description: str) -> None:
    """Stops with a clear message if TradeLab can't listen on host:port."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as error:
            number = getattr(error, "errno", None)
            if number in {48, 98, 10048} or "in use" in str(error).lower():
                raise SetupError(
                    f"Port {port} is already in use ({description}). Stop whatever is using it, or pick another port."
                ) from None
            if number in {13, 10013} or "permission" in str(error).lower():
                raise SetupError(
                    f"This computer won't let TradeLab use port {port} ({description}). Ports below 1024 need "
                    "administrator rights: use a port such as 8443, or run behind Caddy (docs/deployment.md)."
                ) from None
            raise SetupError(f"Couldn't listen on {host}:{port} ({description}): {error}") from None


class ServerGroup:
    """TradeLab's listeners on one asyncio event loop (Hypercorn), sharing a pool of worker threads.

    Hypercorn does TLS handshakes and waits on connections without tying up a
    thread, so idle or slow visitors never hold up anyone else. Each request
    runs on one of `threads` worker threads, for as long as its answer takes
    (a streamed tutor answer can take a couple of minutes).
    """

    def __init__(self, listeners: list[Listener], threads: int = DEFAULT_THREADS):
        self.listeners = listeners
        self.threads = threads
        self._loop = None
        self._stop = None
        self._thread = None
        self._error: BaseException | None = None
        self._loop_ready = threading.Event()

    async def _serve(self) -> None:
        from hypercorn.asyncio import serve

        self._loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="tradelab")
        self._loop.set_default_executor(executor)
        self._stop = asyncio.Event()
        self._loop_ready.set()
        tasks = [
            asyncio.ensure_future(
                serve(for_hypercorn(listener.app), hypercorn_config(listener), shutdown_trigger=self._stop.wait, mode="wsgi")
            )
            for listener in self.listeners
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            self._stop.set()  # one listener stopped (or failed): stop the rest too
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result
        finally:
            executor.shutdown(wait=False)

    def run(self) -> None:
        """Serves in this thread until stop() is called (from a signal handler or another thread)."""
        asyncio.run(self._serve())

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:  # the loop already finished
                pass

    # Running in a background thread, as the tests do:

    def start(self, timeout: float = 10) -> None:
        def target():
            try:
                self.run()
            except BaseException as error:  # noqa: BLE001 - reported by start() or join()
                self._error = error
                self._loop_ready.set()

        self._thread = threading.Thread(target=target, name="tradelab-server", daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout)
        deadline = time.monotonic() + timeout
        for listener in self.listeners:
            while not _accepting(listener.host, listener.port):
                if self._error is not None:
                    raise self._error
                if time.monotonic() > deadline:
                    raise SetupError(f"{listener.name} didn't start listening on port {listener.port}.")
                time.sleep(0.05)

    def join(self, timeout: float | None = 15) -> None:
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._error is not None:
            raise self._error


def _accepting(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in {"0.0.0.0", ""} else "::1" if host == "::" else host
    try:
        with socket.create_connection((target, port), timeout=0.5):
            return True
    except OSError:
        return False


def _urls(settings: Settings, scheme: str, port: int) -> list[str]:
    shown_port = "" if (scheme, port) in {("http", 80), ("https", 443)} else f":{port}"
    if settings.host in {"0.0.0.0", "::", ""}:
        hosts = ["localhost", *lan_addresses()]
    elif ":" in settings.host:
        hosts = [f"[{settings.host}]"]
    else:
        hosts = ["localhost" if settings.host == "127.0.0.1" else settings.host]
    return [f"{scheme}://{host}{shown_port}" for host in hosts]


def run(settings: Settings) -> int:
    # backend.app reads these when it loads, so set them first.
    if settings.db:
        os.environ["TRADELAB_DB"] = str(Path(settings.db).expanduser())
    if settings.model_url:
        os.environ["TRADELAB_LLAMA_URL"] = settings.model_url
    # serve.py applies the proxy setting itself (build_application), so the app mustn't add it again.
    os.environ.pop("TRADELAB_PROXY_HOPS", None)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from backend.app import LLAMA_URL, account_store, app

    try:
        with app.app_context():
            store = account_store()  # opens (or creates) the user database now, so problems show at startup
    except Exception as error:  # noqa: BLE001 - any database problem stops the start with its explanation
        raise SetupError(f"The user database couldn't be opened: {error}") from None

    certificate_note = None
    if settings.https:
        if settings.local_certificate:
            names, addresses = certificate_names(settings.host, settings.extra_names)
            local = ensure_local_certificate(settings.cert_dir, names, addresses)
            settings.cert, settings.key = local.cert, local.key
            certificate_note = (
                f"{'Made' if local.made else 'Using'} a certificate for {', '.join(names + addresses)}, signed by "
                f"TradeLab's local certificate authority. Browsers warn until you trust {local.authority} "
                "on that device (docs/deployment.md shows how)."
            )
        check_certificate(settings.cert, settings.key)

    application = build_application(app, settings.proxy_hops, settings.access_log)
    listeners = [Listener(application, settings.host, settings.port, settings.cert, settings.key)]
    if settings.redirect_port is not None:
        listeners.append(Listener(redirect_to_https(settings.port), settings.host, settings.redirect_port, name="redirect to HTTPS"))
    for listener in listeners:
        check_port_free(listener.host, listener.port, listener.name)

    scheme = "https" if settings.https else "http"
    print(f"\nTradeLab is running on a production server ({settings.threads} worker threads).", flush=True)
    for url in _urls(settings, scheme, settings.port):
        print(f"  {url}", flush=True)
    if settings.host == "127.0.0.1":
        print("  Only this computer can open it. To let other devices in, add --host 0.0.0.0.", flush=True)
    if settings.redirect_port is not None:
        print(f"  Plain HTTP on port {settings.redirect_port} redirects to HTTPS.", flush=True)
    if certificate_note:
        print(f"  {certificate_note}", flush=True)
    if settings.proxy_hops:
        noun = "proxy" if settings.proxy_hops == 1 else "proxies"
        print(f"  Trusting X-Forwarded-* headers from {settings.proxy_hops} reverse {noun}.", flush=True)
    print(f"  Accounts: {store.path}", flush=True)
    print(f"  Tutor model: {LLAMA_URL}", flush=True)
    print("Press Control-C to stop.\n", flush=True)

    group = ServerGroup(listeners, settings.threads)

    def request_stop(*_):
        group.stop()

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), request_stop)
            except (OSError, ValueError):  # not the main thread, or not supported here
                pass
    try:
        group.run()
    except KeyboardInterrupt:
        pass
    except OSError as error:
        raise SetupError(f"The server stopped: {error}") from None
    print("TradeLab stopped.", flush=True)
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("hypercorn.error").setLevel(logging.WARNING)  # serve.py prints its own start-up lines
    try:
        return run(parse_settings(argv))
    except SetupError as error:
        print(f"TradeLab couldn't start: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
