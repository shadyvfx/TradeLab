"""serve.py: the production server, tested over real HTTPS connections.

Starts the servers on free ports with a throwaway local certificate authority, a
throwaway user database and a stand-in for llama-server that streams its
answer a word at a time.
"""

import http.client
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

import backend.app as backend_app
import serve
from backend.app import app
from server.accounts import AccountStore
from server.email_check import EmailCheck

FAST = 1000  # PBKDF2 iterations; real accounts use 1,000,000
PASSWORD = "Tr@deLab2026"
STREAMED_WORDS = 20
WORD_DELAY = 0.05


def accept_all(email):
    return EmailCheck("ok", "ok", None, email.rpartition("@")[2])


def local_session():
    """A requests session that ignores proxy and CA-bundle environment variables."""
    session = requests.Session()
    session.trust_env = False
    return session


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class StreamingModel(BaseHTTPRequestHandler):
    """Answers like llama-server, one word every WORD_DELAY seconds."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for number in range(STREAMED_WORDS):
            chunk = {"choices": [{"delta": {"content": f"word{number} "}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(WORD_DELAY)
        done = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()


class Running:
    """One serve.py listener on a free port, served from a background thread."""

    def __init__(self, application, cert=None, key=None):
        self.port = free_port()
        self.group = serve.ServerGroup([serve.Listener(application, "127.0.0.1", self.port, cert, key)], threads=8)
        self.group.start()

    def stop(self):
        self.group.join()


class ProductionServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        folder = Path(cls.folder.name)
        local = serve.ensure_local_certificate(folder / "certs", ["localhost"], ["127.0.0.1"])
        cls.cert, cls.key, cls.authority = local.cert, local.key, local.authority

        cls.model = ThreadingHTTPServer(("127.0.0.1", 0), StreamingModel)
        threading.Thread(target=cls.model.serve_forever, daemon=True).start()
        cls.saved_llama_url = backend_app.LLAMA_URL
        backend_app.LLAMA_URL = f"http://127.0.0.1:{cls.model.server_address[1]}/v1/chat/completions"

        cls.saved_limits = app.config.get("RATE_LIMITS")
        app.config["RATE_LIMITS"] = {}
        app.config["TESTING"] = True

        cls.https = Running(serve.build_application(app, log_requests=False), cls.cert, cls.key)
        cls.proxied = Running(serve.build_application(app, proxy_hops=1, log_requests=False))
        cls.redirect = Running(serve.redirect_to_https(cls.https.port))
        cls.base = f"https://localhost:{cls.https.port}"

    @classmethod
    def tearDownClass(cls):
        for running in (cls.https, cls.proxied, cls.redirect):
            running.stop()
        cls.model.shutdown()
        cls.model.server_close()
        backend_app.LLAMA_URL = cls.saved_llama_url
        app.config["RATE_LIMITS"] = cls.saved_limits
        cls.folder.cleanup()

    def setUp(self):
        self.store = AccountStore(os.path.join(self.folder.name, f"{self.id()}.db"), iterations=FAST, email_checker=accept_all)
        self.store.migrate()
        app.config["ACCOUNT_STORE"] = self.store
        self.session = local_session()
        self.session.verify = str(self.authority)  # trust the local authority, like a device would
        self.session.headers["Origin"] = self.base

    def tearDown(self):
        self.session.close()
        app.config.pop("ACCOUNT_STORE", None)

    def sign_up(self, session=None, base=None, **headers):
        return (session or self.session).post(
            f"{base or self.base}/api/users",
            json={"username": "ana_b", "email": "ana@gmail.com", "password": PASSWORD},
            headers=headers or None,
            timeout=10,
        )

    # -- HTTPS ---------------------------------------------------------------

    def test_the_site_loads_over_https_with_its_security_headers(self):
        response = self.session.get(f"{self.base}/", timeout=10)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>TradeLab</title>", response.text)
        self.assertIn("script-src 'self' 'sha256-", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Strict-Transport-Security", response.headers)  # never for localhost

        script = self.session.get(f"{self.base}/js/app.js", timeout=10)
        self.assertEqual(script.status_code, 200)
        self.assertTrue(script.headers["Content-Type"].startswith("text/javascript"))

    def test_responses_without_a_body_work(self):
        # 204, 304 and HEAD responses have no body; the server must still send them.
        self.assertEqual(self.session.head(f"{self.base}/", timeout=10).status_code, 200)
        first = self.session.get(f"{self.base}/css/tokens.css", timeout=10)
        cached = self.session.get(f"{self.base}/css/tokens.css", headers={"If-None-Match": first.headers["ETag"]}, timeout=10)
        self.assertEqual(cached.status_code, 304)
        with local_session() as session:
            head = session.head(f"http://localhost:{self.redirect.port}/", allow_redirects=False, timeout=10)
            self.assertEqual(head.status_code, 301)

    def test_only_tls_1_2_and_newer_are_offered_with_http2(self):
        listener = serve.Listener(None, "127.0.0.1", self.https.port, self.cert, self.key)
        server_context = serve.hypercorn_config(listener).create_ssl_context()
        self.assertEqual(server_context.minimum_version, ssl.TLSVersion.TLSv1_2)
        context = ssl.create_default_context(cafile=str(self.authority))
        context.set_alpn_protocols(["h2", "http/1.1"])
        with socket.create_connection(("127.0.0.1", self.https.port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as tls:
                self.assertIn(tls.version(), {"TLSv1.2", "TLSv1.3"})
                self.assertEqual(tls.selected_alpn_protocol(), "h2")

    def test_accounts_work_over_https_with_a_secure_cookie(self):
        created = self.sign_up()
        self.assertEqual(created.status_code, 201)
        cookie = created.headers["Set-Cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(flag, cookie)

        self.assertEqual(self.session.get(f"{self.base}/api/session", timeout=10).json()["user"]["username"], "ana_b")
        progress = {"schemaVersion": 1, "state": {"learning": {"completed": ["what-is-a-stock"]}}}
        saved = self.session.put(f"{self.base}/api/me/data", json={"data": progress}, timeout=10)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.session.get(f"{self.base}/api/me/data", timeout=10).json()["data"], progress)

        self.assertEqual(self.session.delete(f"{self.base}/api/session", timeout=10).status_code, 204)
        self.assertIsNone(self.session.get(f"{self.base}/api/session", timeout=10).json()["user"])

        signed_in = self.session.post(f"{self.base}/api/session", json={"login": "ana_b", "password": PASSWORD}, timeout=10)
        self.assertEqual(signed_in.status_code, 200)
        self.assertEqual(self.session.get(f"{self.base}/api/me/data", timeout=10).json()["data"], progress)

    def test_guest_limits_and_lessons_work_over_https(self):
        session = self.session.get(f"{self.base}/api/session", timeout=10)
        self.assertIsNone(session.json()["user"])
        self.assertEqual(session.json()["guestLimits"]["simulationDaysRemaining"], 21)
        guest_cookie = session.headers["Set-Cookie"]
        self.assertTrue(guest_cookie.startswith("tradelab_guest="))
        for flag in ("Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(flag, guest_cookie)

        advanced = self.session.post(f"{self.base}/api/guest/simulation/advance", json={"days": 5}, timeout=10)
        self.assertEqual(advanced.status_code, 200)
        self.assertEqual(advanced.json()["limits"]["simulationDaysRemaining"], 16)
        self.assertEqual(self.session.post(f"{self.base}/api/guest/journal/entries", json={}, timeout=10).status_code, 200)

        self.assertEqual(self.session.get(f"{self.base}/api/lessons/what-is-a-stock", timeout=10).status_code, 200)
        locked = self.session.get(f"{self.base}/api/lessons/candlestick-anatomy", timeout=10)
        self.assertEqual((locked.status_code, locked.json()["error"]["code"]), (403, "course_account_required"))
        self.assertEqual(self.sign_up().status_code, 201)
        unlocked = self.session.get(f"{self.base}/api/lessons/candlestick-anatomy", timeout=10)
        self.assertEqual(unlocked.status_code, 200)
        self.assertEqual(unlocked.json()["lesson"]["id"], "candlestick-anatomy")

    def test_the_email_checker_answers_over_https(self):
        response = self.session.post(f"{self.base}/api/email-check", json={"email": "ana@gmail.com"}, timeout=10)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_other_websites_cannot_sign_in(self):
        response = self.session.post(
            f"{self.base}/api/session",
            json={"login": "ana_b", "password": PASSWORD},
            headers={"Origin": "https://evil.example"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden_origin")

    def test_tutor_answers_stream_over_https_as_they_are_written(self):
        question = {"messages": [{"role": "user", "content": "Explain market orders"}], "stream": True}
        # The tutor answers signed-in accounts only.
        guest = self.session.post(f"{self.base}/api/tutor/chat", json=question, timeout=10)
        self.assertEqual(guest.status_code, 401)
        self.assertEqual(guest.json()["error"]["code"], "account_required")
        self.assertEqual(self.sign_up().status_code, 201)

        started = time.monotonic()
        arrivals, text = [], ""
        with self.session.post(
            f"{self.base}/api/tutor/chat",
            json=question,
            stream=True,
            timeout=30,
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["Content-Type"].startswith("text/event-stream"))
            event = None
            for line in response.iter_lines(decode_unicode=True):
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event == "token":
                    arrivals.append(time.monotonic() - started)
                    text += json.loads(line[6:])["content"]
        self.assertEqual(len(arrivals), STREAMED_WORDS)
        self.assertIn("word0", text)
        self.assertIn(f"word{STREAMED_WORDS - 1}", text)
        # Streamed, not buffered: the first word arrives long before the last.
        self.assertGreater(arrivals[-1] - arrivals[0], STREAMED_WORDS * WORD_DELAY * 0.6)

    def test_plain_http_on_the_https_port_is_not_served(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.https.port, timeout=5)
        try:
            with self.assertRaises((http.client.HTTPException, ConnectionError, OSError)):
                connection.request("GET", "/")
                connection.getresponse().read()
        finally:
            connection.close()

    def test_an_idle_connection_doesnt_hold_up_other_visitors(self):
        idle = [socket.create_connection(("127.0.0.1", self.https.port)) for _ in range(5)]  # no TLS handshake
        try:
            started = time.monotonic()
            self.assertEqual(self.session.get(f"{self.base}/api/health", timeout=10).status_code, 200)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            for connection in idle:
                connection.close()

    def test_oversized_requests_are_refused(self):
        self.assertEqual(self.sign_up().status_code, 201)
        headers = {"Content-Type": "application/json"}
        too_big_for_the_app = b'{"data": "' + b"x" * (3 * 1024 * 1024) + b'"}'
        response = self.session.put(f"{self.base}/api/me/data", data=too_big_for_the_app, headers=headers, timeout=30)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "payload_too_large")
        too_big_for_the_server = b"x" * (serve.MAX_REQUEST_BODY_BYTES + 1024)
        try:
            response = self.session.put(f"{self.base}/api/me/data", data=too_big_for_the_server, headers=headers, timeout=30)
        except requests.ConnectionError:
            return  # the server hung up on the upload: also refused
        self.assertEqual(response.status_code, 400)

    # -- Redirect --------------------------------------------------------------

    def test_plain_http_visitors_are_sent_to_https(self):
        base = f"http://localhost:{self.redirect.port}"
        with local_session() as session:
            page = session.get(f"{base}/lessons?topic=charts", allow_redirects=False, timeout=10)
            self.assertEqual(page.status_code, 301)
            self.assertEqual(page.headers["Location"], f"https://localhost:{self.https.port}/lessons?topic=charts")
            form = session.post(f"{base}/api/session", json={}, allow_redirects=False, timeout=10)
            self.assertEqual(form.status_code, 308)  # keeps the method and body

    # -- Behind a reverse proxy -----------------------------------------------

    def test_behind_a_proxy_the_visitor_sees_https(self):
        session = local_session()
        base = f"http://127.0.0.1:{self.proxied.port}"
        forwarded = {
            "X-Forwarded-For": "203.0.113.9",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "tradelab.example.com",
            "Origin": "https://tradelab.example.com",
        }
        created = self.sign_up(session=session, base=base, **forwarded)
        self.assertEqual(created.status_code, 201)
        self.assertIn("Secure", created.headers["Set-Cookie"])
        self.assertIn("max-age=", created.headers["Strict-Transport-Security"])
        session.close()


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = serve.parse_settings([], environ={})
        self.assertEqual((settings.host, settings.port, settings.https, settings.proxy_hops), ("127.0.0.1", 5001, False, 0))

    def test_https_defaults_to_port_8443_with_a_certificate_made_for_this_computer(self):
        settings = serve.parse_settings(["--https"], environ={})
        self.assertEqual(settings.port, 8443)
        self.assertTrue(settings.local_certificate)

    def test_hosting_platforms_set_the_port_and_proxy_with_environment_variables(self):
        settings = serve.parse_settings([], environ={"PORT": "10000", "TRADELAB_PROXY_HOPS": "1", "TRADELAB_HOST": "0.0.0.0"})
        self.assertEqual((settings.host, settings.port, settings.proxy_hops), ("0.0.0.0", 10000, 1))

    def test_behind_proxy_alone_means_one_proxy(self):
        self.assertEqual(serve.parse_settings(["--behind-proxy"], environ={}).proxy_hops, 1)
        self.assertEqual(serve.parse_settings(["--behind-proxy", "2"], environ={}).proxy_hops, 2)

    def test_a_certificate_needs_its_key(self):
        with self.assertRaises(serve.SetupError):
            serve.parse_settings(["--cert", "fullchain.pem"], environ={})
        settings = serve.parse_settings(["--cert", "fullchain.pem", "--key", "privkey.pem"], environ={})
        self.assertTrue(settings.https)
        self.assertFalse(settings.local_certificate)

    def test_redirecting_needs_https_and_its_own_port(self):
        with self.assertRaises(serve.SetupError):
            serve.parse_settings(["--redirect-port", "80"], environ={})
        with self.assertRaises(serve.SetupError):
            serve.parse_settings(["--https", "--port", "8443", "--redirect-port", "8443"], environ={})

    def test_model_addresses(self):
        self.assertEqual(serve.chat_completions_url("192.168.1.23:8080"), "http://192.168.1.23:8080/v1/chat/completions")
        self.assertEqual(serve.chat_completions_url("http://gpu.lan:8080/"), "http://gpu.lan:8080/v1/chat/completions")
        full = "http://127.0.0.1:8080/v1/chat/completions"
        self.assertEqual(serve.chat_completions_url(full), full)


class CertificateTests(unittest.TestCase):
    def test_the_certificate_is_reused_until_it_no_longer_fits(self):
        from cryptography import x509

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "certs"
            first = serve.ensure_local_certificate(directory, ["localhost"], ["127.0.0.1"])
            self.assertTrue(first.made)
            if os.name == "posix":
                for private in (first.key, directory / "tradelab-local-ca-key.pem"):
                    self.assertEqual(private.stat().st_mode & 0o777, 0o600)
            authority = x509.load_pem_x509_certificate(first.authority.read_bytes())
            certificate = x509.load_pem_x509_certificate(first.cert.read_bytes())
            certificate.verify_directly_issued_by(authority)
            self.assertTrue(authority.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            self.assertFalse(certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)

            again = serve.ensure_local_certificate(directory, ["localhost"], ["127.0.0.1"])
            self.assertFalse(again.made)

            # A new network address gets a new certificate from the same authority,
            # so devices that already trust the authority don't warn.
            moved = serve.ensure_local_certificate(directory, ["localhost"], ["127.0.0.1", "192.168.50.7"])
            self.assertTrue(moved.made)
            self.assertEqual(x509.load_pem_x509_certificate(moved.authority.read_bytes()), authority)
            x509.load_pem_x509_certificate(moved.cert.read_bytes()).verify_directly_issued_by(authority)

    def test_the_certificate_names_this_computer(self):
        names, addresses = serve.certificate_names("0.0.0.0", ["tradelab.test", "10.0.0.9"])
        self.assertIn("localhost", names)
        self.assertIn("tradelab.test", names)
        self.assertIn("127.0.0.1", addresses)
        self.assertIn("10.0.0.9", addresses)


if __name__ == "__main__":
    unittest.main()
