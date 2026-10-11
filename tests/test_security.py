"""Security headers, rate limits and proxy handling in the Flask app (backend/security.py)."""

import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from backend.app import PROJECT_ROOT, app
from backend.security import (
    DEFAULT_RATE_LIMITS,
    RateLimiter,
    client_key,
    content_security_policy,
    inline_script_hashes,
    is_public_hostname,
)
from server.accounts import AccountStore
from server.email_check import EmailCheck

FAST = 1000  # PBKDF2 iterations; real accounts use 1,000,000
PASSWORD = "Tr@deLab2026"
VISITOR = "198.51.100.7"


def accept_all(email):
    return EmailCheck("ok", "ok", None, email.rpartition("@")[2])


class SecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = AccountStore(os.path.join(self.folder.name, "tradelab.db"), iterations=FAST, email_checker=accept_all)
        self.store.migrate()
        app.config["ACCOUNT_STORE"] = self.store
        app.config["TESTING"] = True
        self.saved_limits = app.config.get("RATE_LIMITS")
        app.extensions["tradelab_rate_limiter"].reset()

    def tearDown(self):
        app.config["RATE_LIMITS"] = self.saved_limits
        app.extensions["tradelab_rate_limiter"].reset()
        app.config.pop("ACCOUNT_STORE", None)
        self.folder.cleanup()

    @staticmethod
    def request_from(address, path, method="GET", base_url="http://localhost", **kwargs):
        """A request from another computer (`address`), to the site at `base_url`."""
        response = app.test_client().open(path, method=method, base_url=base_url, environ_base={"REMOTE_ADDR": address}, **kwargs)
        response.close()  # the headers, status and data stay readable
        return response

    @staticmethod
    def get(path):
        response = app.test_client().get(path)
        response.close()
        return response


class HeaderTests(SecurityTestCase):
    def test_every_response_has_the_security_headers(self):
        for path in ("/", "/js/app.js", "/api/health", "/api/session"):
            with self.subTest(path=path):
                headers = self.get(path).headers
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["Referrer-Policy"], "same-origin")
                self.assertIn("camera=()", headers["Permissions-Policy"])

    def test_the_page_only_runs_its_own_scripts(self):
        policy = self.get("/").headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertNotIn("unsafe-eval", policy)
        script_src = next(part for part in policy.split("; ") if part.startswith("script-src "))
        self.assertNotIn("unsafe-inline", script_src)

    def test_the_inline_theme_script_is_allowed_by_its_hash(self):
        html = (PROJECT_ROOT / "index.html").read_bytes()
        start = html.index(b"<script>") + len(b"<script>")
        end = html.index(b"</script>", start)
        expected = "'sha256-" + base64.b64encode(hashlib.sha256(html[start:end]).digest()).decode() + "'"
        self.assertIn(expected, self.get("/").headers["Content-Security-Policy"])

    def test_windows_line_endings_hash_like_the_browser_sees_them(self):
        unix = b"<script>\n  var a = 1;\n</script>"
        windows = unix.replace(b"\n", b"\r\n")
        self.assertEqual(inline_script_hashes(unix), inline_script_hashes(windows))
        self.assertEqual(inline_script_hashes(b'<script src="x.js"></script>'), [])

    def test_the_policy_follows_edits_to_index_html(self):
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "index.html"
            page.write_bytes(b"<script>one()</script>")
            first = content_security_policy(page)
            page.write_bytes(b"<script>two();</script>")
            self.assertNotEqual(first, content_security_policy(page))

    def test_api_answers_are_never_cached(self):
        self.assertEqual(self.get("/api/health").headers["Cache-Control"], "no-store")
        self.assertNotEqual(self.get("/js/app.js").headers.get("Cache-Control"), "no-store")

    def test_health_check(self):
        response = self.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_hsts_only_for_https_on_a_real_domain(self):
        cases = {
            "https://tradelab.example.com": True,
            "https://localhost:8443": False,
            "https://192.168.1.20:8443": False,
            "https://my-mac.local:8443": False,
            "http://tradelab.example.com": False,
        }
        for base_url, expected in cases.items():
            with self.subTest(base_url=base_url):
                headers = self.request_from("127.0.0.1", "/", base_url=base_url).headers
                self.assertEqual("Strict-Transport-Security" in headers, expected)

    def test_public_hostnames(self):
        for host in ("tradelab.example.com", "tradelab.example.com:443", "www.tradelab.app"):
            self.assertTrue(is_public_hostname(host), host)
        for host in ("localhost", "localhost:5001", "127.0.0.1", "[::1]:8443", "my-mac.local", "box.lan", "host", "", "::1"):
            self.assertFalse(is_public_hostname(host), host)


class RateLimitTests(SecurityTestCase):
    def sign_in(self, address):
        return self.request_from(address, "/api/session", method="POST", json={"login": "nobody", "password": PASSWORD})

    def test_too_many_sign_ins_from_one_network_wait(self):
        app.config["RATE_LIMITS"] = {("POST", "/api/session"): (2, 60, "Slow down, please.")}
        self.assertEqual(self.sign_in(VISITOR).status_code, 401)
        self.assertEqual(self.sign_in(VISITOR).status_code, 401)
        blocked = self.sign_in(VISITOR)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.get_json()["error"], {"code": "rate_limited", "message": "Slow down, please."})
        self.assertGreaterEqual(int(blocked.headers["Retry-After"]), 1)
        self.assertEqual(blocked.headers["Cache-Control"], "no-store")
        # Another network still gets in.
        self.assertEqual(self.sign_in("203.0.113.50").status_code, 401)

    def test_this_computer_is_never_limited(self):
        app.config["RATE_LIMITS"] = {("POST", "/api/session"): (1, 60, "Slow down.")}
        for address in ("127.0.0.1", "::1"):
            for _ in range(3):
                self.assertEqual(self.sign_in(address).status_code, 401)

    def test_ipv6_visitors_share_their_network_allowance(self):
        app.config["RATE_LIMITS"] = {("POST", "/api/session"): (1, 60, "Slow down.")}
        self.assertEqual(self.sign_in("2001:db8:1:2::10").status_code, 401)
        self.assertEqual(self.sign_in("2001:db8:1:2::99").status_code, 429)
        self.assertEqual(self.sign_in("2001:db8:1:3::10").status_code, 401)

    def test_only_listed_endpoints_are_limited(self):
        app.config["RATE_LIMITS"] = {("POST", "/api/session"): (1, 60, "Slow down.")}
        for _ in range(3):
            self.assertEqual(self.request_from(VISITOR, "/api/session").status_code, 200)

    def test_default_limits_cover_passwords_signups_email_checks_and_the_tutor(self):
        self.assertEqual(
            set(DEFAULT_RATE_LIMITS),
            {("POST", "/api/session"), ("POST", "/api/users"), ("POST", "/api/email-check"), ("POST", "/api/tutor/chat")},
        )
        for limit, window, message in DEFAULT_RATE_LIMITS.values():
            self.assertGreaterEqual(limit, 20)  # a classroom on one Wi-Fi network still fits
            self.assertTrue(message.endswith("."))

    def test_the_window_slides(self):
        now = [1000.0]
        limiter = RateLimiter(clock=lambda: now[0])
        self.assertEqual(limiter.hit(("k",), 2, 10), 0)
        now[0] += 4
        self.assertEqual(limiter.hit(("k",), 2, 10), 0)
        self.assertEqual(limiter.hit(("k",), 2, 10), 6)  # the first hit leaves the window in 6 s
        now[0] += 6
        self.assertEqual(limiter.hit(("k",), 2, 10), 0)

    def test_client_keys(self):
        self.assertIsNone(client_key("127.0.0.1"))
        self.assertIsNone(client_key("::1"))
        self.assertEqual(client_key("::ffff:198.51.100.7"), "198.51.100.7")
        self.assertEqual(client_key("2001:db8::1"), "2001:db8::/64")


class ProxyTests(SecurityTestCase):
    FORWARDED = {
        "X-Forwarded-For": VISITOR,
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "tradelab.example.com",
    }

    def test_forwarded_headers_are_ignored_unless_proxy_mode_is_on(self):
        response = app.test_client().post(
            "/api/users",
            json={"username": "ana_b", "email": "ana@gmail.com", "password": PASSWORD},
            headers=self.FORWARDED,
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("Secure", response.headers["Set-Cookie"])
        self.assertNotIn("Strict-Transport-Security", response.headers)


if __name__ == "__main__":
    unittest.main()
