"""Production safeguards for the Flask app: security headers, rate limits and proxy support.

backend/app.py applies these, so they hold under any server (serve.py,
flask run, gunicorn).

- Security headers on every response, and a Content-Security-Policy on the
  page that only runs TradeLab's own scripts. The inline theme script in
  index.html is allowed by its hash, worked out from the file itself, so
  editing that script doesn't break the page.
- Strict-Transport-Security on HTTPS responses for real domain names. Never
  for localhost, .local names or IP addresses, where it would stop plain-HTTP
  development on the same computer from working.
- Rate limits per network on sign-in, sign-up, the email checker and the
  tutor, so nobody can guess passwords quickly or tie up the model. Requests
  from this computer itself (localhost) are never limited.
- Behind a reverse proxy (Caddy, nginx, a hosting platform), trusting its
  X-Forwarded-For, -Proto and -Host headers, so secure cookies, the same-site
  check and the rate limits see each visitor's real address and HTTPS.
  serve.py --behind-proxy does this; with another server, set
  TRADELAB_PROXY_HOPS=1.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import math
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

from flask import jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = PROJECT_ROOT / "index.html"

# (method, path): (requests allowed, in this many seconds, message for the visitor)
DEFAULT_RATE_LIMITS = {
    ("POST", "/api/session"): (
        20,
        5 * 60,
        "Too many sign-in attempts from your network. Wait a few minutes, then try again.",
    ),
    ("POST", "/api/users"): (
        20,
        60 * 60,
        "Too many new accounts from your network. Try again later.",
    ),
    ("POST", "/api/email-check"): (
        120,
        5 * 60,
        "Too many email checks from your network. Wait a moment, then try again.",
    ),
    ("POST", "/api/tutor/chat"): (
        40,
        5 * 60,
        "The tutor has had a lot of questions from your network. Wait a minute, then ask again.",
    ),
}

HSTS_MAX_AGE = 180 * 24 * 60 * 60  # 180 days
LOCAL_NAME_SUFFIXES = (".localhost", ".local", ".lan", ".home.arpa", ".internal")
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
INLINE_SCRIPT = re.compile(rb"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_off(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"0", "off", "false", "no"}


def trust_proxy(wsgi_app, hops: int = 1):
    """Wraps a WSGI app to trust X-Forwarded-* headers from `hops` reverse proxies."""
    if hops < 1:
        return wsgi_app
    return ProxyFix(wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_port=hops)


# -- Rate limits -------------------------------------------------------------


class RateLimiter:
    """Counts requests per key in a sliding window. In memory, for one server process."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._hits: dict[tuple, tuple[deque, float]] = {}
        self._lock = threading.Lock()
        self._checks = 0

    def hit(self, key: tuple, limit: int, window: float) -> int:
        """Records a request. Returns 0 if it's allowed, or the seconds to wait if not."""
        now = self._clock()
        with self._lock:
            hits, _ = self._hits.get(key) or (deque(), window)
            while hits and hits[0] <= now - window:
                hits.popleft()
            self._checks += 1
            if self._checks % 1000 == 0:
                self._forget_idle(now)
            if len(hits) >= limit:
                self._hits[key] = (hits, window)
                return max(1, math.ceil(hits[0] + window - now))
            hits.append(now)
            self._hits[key] = (hits, window)
            return 0

    def _forget_idle(self, now: float) -> None:
        for key in [key for key, (hits, window) in self._hits.items() if not hits or hits[-1] <= now - window]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def client_key(address: str | None) -> str | None:
    """The network a request comes from, or None for this computer (never limited).

    IPv6 visitors are grouped by /64, since one home network gets a whole /64.
    """
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return address or "unknown"
    if ip.is_loopback:
        return None
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return client_key(str(ip.ipv4_mapped))
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


# -- Headers -----------------------------------------------------------------


def is_public_hostname(host: str) -> bool:
    """True for a domain name on the internet; False for localhost, .local names and IP addresses."""
    name = (host or "").strip().lower()
    if not name or name.startswith("["):
        return False
    if name.count(":") == 1:
        name = name.rsplit(":", 1)[0]
    elif name.count(":") > 1:
        return False
    name = name.rstrip(".")
    if "." not in name or name == "localhost" or name.endswith(LOCAL_NAME_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return True
    return False


_csp_cache: dict = {"key": None, "value": None}


def inline_script_hashes(html: bytes) -> list[str]:
    """CSP hashes for the page's inline scripts, as the browser computes them."""
    hashes = []
    for script in INLINE_SCRIPT.findall(html):
        # Browsers turn CRLF and CR into LF before running (and hashing) a script.
        text = script.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(text).digest()).decode("ascii") + "'")
    return hashes


def content_security_policy(index_html: Path = INDEX_HTML) -> str:
    try:
        stat = index_html.stat()
        key = (str(index_html), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(index_html), None, None)
    if _csp_cache["key"] != key:
        try:
            hashes = inline_script_hashes(index_html.read_bytes())
        except OSError:
            hashes = []
        _csp_cache["value"] = "; ".join(
            (
                "default-src 'self'",
                "script-src " + " ".join(["'self'", *hashes]),
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data: blob:",
                "font-src 'self'",
                "connect-src 'self'",
                "manifest-src 'self'",
                "worker-src 'self' blob:",
                "object-src 'none'",
                "base-uri 'self'",
                "form-action 'self'",
                "frame-ancestors 'none'",
            )
        )
        _csp_cache["key"] = key
    return _csp_cache["value"]


# -- Setup -------------------------------------------------------------------


def init_security(app) -> None:
    """Adds the security headers, rate limits and (when configured) proxy support to `app`."""
    hops = _env_int("TRADELAB_PROXY_HOPS", 0)
    if hops > 0:
        app.wsgi_app = trust_proxy(app.wsgi_app, hops)

    app.config.setdefault("RATE_LIMITS", {} if _env_off("TRADELAB_RATE_LIMITS") else dict(DEFAULT_RATE_LIMITS))
    limiter = RateLimiter()
    app.extensions["tradelab_rate_limiter"] = limiter

    @app.before_request
    def _rate_limit():
        rule = (app.config.get("RATE_LIMITS") or {}).get((request.method, request.path))
        if not rule:
            return None
        network = client_key(request.remote_addr)
        if network is None:
            return None
        limit, window, message = rule
        wait = limiter.hit((request.method, request.path, network), limit, window)
        if not wait:
            return None
        response = jsonify(error={"code": "rate_limited", "message": message})
        response.status_code = 429
        response.headers["Retry-After"] = str(wait)
        return response

    @app.after_request
    def _security_headers(response):
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        if response.mimetype == "text/html":
            headers.setdefault("Content-Security-Policy", content_security_policy())
        if (request.path == "/api" or request.path.startswith("/api/")) and "Cache-Control" not in headers:
            headers["Cache-Control"] = "no-store"
        if request.is_secure and is_public_hostname(request.host):
            headers.setdefault("Strict-Transport-Security", f"max-age={HSTS_MAX_AGE}")
        return response
