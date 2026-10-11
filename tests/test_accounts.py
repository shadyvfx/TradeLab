"""Tests for the account server: rules, password hashing, the user database,
the email checker and the HTTP endpoints. Run from the project folder with:

    python3 -m unittest discover -s tests -v

Standard library only, and no network: DNS replies come from canned packets
and a stand-in DNS server on this machine. The rule checks use
tests/account-cases.json, the same cases tests/core.test.mjs runs against the
browser's copy of the rules.
"""

import importlib.util
import json
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.cookiejar import CookieJar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.accounts import (  # noqa: E402
    AccountError,
    AccountStore,
    DatabaseSetupError,
    hash_password,
    validate_registration,
    validate_sign_in,
    verify_password,
)
from server.email_check import (  # noqa: E402
    CANARY_DOMAIN,
    EmailCheck,
    EmailChecker,
    build_query,
    lookup_mx,
    parse_mx_response,
    typo_suggestion,
)

with open(os.path.join(ROOT, "tests", "account-cases.json"), encoding="utf-8") as handle:
    CASES = json.load(handle)

FAST = 1000  # PBKDF2 iterations for tests; the server uses 1,000,000


def accept_all(email):
    """Stand-in email checker for tests that aren't about email delivery."""
    return EmailCheck("ok", "ok", None, email.rpartition("@")[2])


# Real replies from a public DNS server to MX queries made with build_query(name, 0x1234).
DNS_REPLIES = {name: bytes.fromhex(packet) for name, packet in {
    "gmail.com": "12348180000100050000000105676d61696c03636f6d00000f0001c00c000f00010000033c0020001e04616c74330d676d61696c2d736d74702d696e016c06676f6f676c65c012c00c000f00010000033c0009001404616c7432c02ec00c000f00010000033c0009000a04616c7431c02ec00c000f00010000033c00040005c02ec00c000f00010000033c0009002804616c7434c02e0000290200000000000000",
    "example.com": "123481800001000100000001076578616d706c6503636f6d00000f0001c00c000f00010000012c00030000000000290200000000000000",
    "no-such-domain-4815.com": "123481830001000000010001136e6f2d737563682d646f6d61696e2d3438313503636f6d00000f0001c0200006000100000384003d01610c67746c642d73657276657273036e657400056e73746c640c766572697369676e2d677273c0206ac9763b000007080000038400093a80000003840000290200000000000000",
    "github.io": "1234818000010000000100010667697468756202696f00000f0001c00c00060001000002af003504646e733103703035056e736f6e65036e6574000a686f73746d6173746572c0306234c3d10000a8c000001c200012750000000e100000290200000000000000",
}.items()}

# Made with Werkzeug 3.1: generate_password_hash("Tr@deLab1", method=...)
WERKZEUG_PBKDF2 = "pbkdf2:sha256:1000$0NqUP1mSHIiGKCVZ$0a54090ca767af4022af1b9202334c5ef0fb26927850c862f212bcb6353b0ec0"
WERKZEUG_SCRYPT = (
    "scrypt:1024:8:1$PvDOGH64ooxLCNr8$7022db84f436a5dc66255ee41cf78c20baa1b1700a84c2efefb188260aa87378"
    "9df794bba29b4d415a3b2b18b8f8ad145acf6147e7c197068d75e7ba82a47001"
)


class RuleTests(unittest.TestCase):
    def test_registration_cases(self):
        for case in CASES["registration"]:
            with self.subTest(case["name"]):
                errors, value = validate_registration(case["input"])
                self.assertEqual(errors, case["errors"])
                if "value" in case:
                    self.assertEqual(value["username"], case["value"]["username"])
                    self.assertEqual(value["email"], case["value"]["email"])

    def test_sign_in_cases(self):
        for case in CASES["signIn"]:
            with self.subTest(case["name"]):
                errors, value = validate_sign_in(case["input"])
                self.assertEqual(errors, case["errors"])
                if "value" in case:
                    self.assertEqual(value["login"], case["value"]["login"])


class PasswordHashTests(unittest.TestCase):
    def test_round_trip_and_format(self):
        stored = hash_password("Tr@deLab1", FAST)
        self.assertTrue(stored.startswith("pbkdf2:sha256:1000$"))
        self.assertNotIn("Tr@deLab1", stored)
        self.assertTrue(verify_password(stored, "Tr@deLab1"))
        self.assertFalse(verify_password(stored, "tr@deLab1"))

    def test_salts_differ(self):
        self.assertNotEqual(hash_password("Tr@deLab1", FAST), hash_password("Tr@deLab1", FAST))

    def test_reads_werkzeug_hashes(self):
        self.assertTrue(verify_password(WERKZEUG_PBKDF2, "Tr@deLab1"))
        self.assertFalse(verify_password(WERKZEUG_PBKDF2, "Tr@deLab2"))
        self.assertTrue(verify_password(WERKZEUG_SCRYPT, "Tr@deLab1"))
        self.assertFalse(verify_password(WERKZEUG_SCRYPT, "nope"))

    def test_rejects_malformed_hashes(self):
        for stored in ["", "plain-text", "pbkdf2:sha256$a$b", "md5$a$b", "pbkdf2:nohash:10$a$b"]:
            self.assertFalse(verify_password(stored, "Tr@deLab1"), stored)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "tradelab.db")
        self.store = AccountStore(self.path, iterations=FAST, email_checker=accept_all)

    def tearDown(self):
        self.folder.cleanup()

    def columns(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return [row[1] for row in connection.execute("PRAGMA table_info(users)")]

    def test_new_database(self):
        self.assertEqual(self.store.migrate(), ["Created the users table."])
        self.assertIn("username", self.columns())
        self.assertEqual(self.store.migrate(), [])  # running again changes nothing
        self.assertEqual(self.store.count_users(), 0)

    def test_create_user(self):
        self.store.migrate()
        user = self.store.create_user("ay_0b", "Ay@Example.com", "Tr@deLab1")
        self.assertEqual(user["username"], "ay_0b")
        self.assertEqual(user["email"], "ay@example.com")
        self.assertNotIn("password_hash", user)
        with closing(sqlite3.connect(self.path)) as connection:
            display_name, stored = connection.execute("SELECT display_name, password_hash FROM users").fetchone()
        self.assertEqual(display_name, "ay_0b")
        self.assertTrue(stored.startswith("pbkdf2:sha256:"))
        self.assertNotIn("Tr@deLab1", stored)

    def test_rejects_invalid_input(self):
        self.store.migrate()
        with self.assertRaises(AccountError) as caught:
            self.store.create_user("ay rannan", "not-an-email", "weak")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(set(caught.exception.fields), {"username", "email", "password"})
        self.assertEqual(self.store.count_users(), 0)

    def test_duplicates_ignore_case(self):
        self.store.migrate()
        self.store.create_user("Ayoub", "ay@example.com", "Tr@deLab1")
        with self.assertRaises(AccountError) as caught:
            self.store.create_user("ayoub", "other@example.com", "Tr@deLab1")
        self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.field), (409, "username_taken", "username"))
        with self.assertRaises(AccountError) as caught:
            self.store.create_user("someone", "AY@EXAMPLE.COM", "Tr@deLab1")
        self.assertEqual((caught.exception.code, caught.exception.field), ("email_taken", "email"))
        self.assertEqual(self.store.count_users(), 1)

    def test_sign_in_with_username_or_email(self):
        self.store.migrate()
        self.store.create_user("ay_0b", "ay@example.com", "Tr@deLab1")
        self.assertEqual(self.store.authenticate("AY_0B", "Tr@deLab1")["username"], "ay_0b")
        self.assertEqual(self.store.authenticate(" Ay@Example.com ", "Tr@deLab1")["username"], "ay_0b")
        for login, password in [("ay_0b", "tr@deLab1"), ("nobody", "Tr@deLab1"), ("no@example.com", "Tr@deLab1")]:
            with self.assertRaises(AccountError) as caught:
                self.store.authenticate(login, password)
            self.assertEqual((caught.exception.status, caught.exception.code), (401, "invalid_credentials"))

    def test_sessions(self):
        self.store.migrate()
        user = self.store.create_user("ay_0b", "ay@example.com", "Tr@deLab1")
        token = self.store.create_session(user["id"])
        self.assertEqual(self.store.user_for_session(token)["id"], user["id"])
        self.assertIsNone(self.store.user_for_session("not-a-token"))
        self.assertIsNone(self.store.user_for_session(None))
        with closing(sqlite3.connect(self.path)) as connection:
            stored = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertNotEqual(stored, token)  # only a hash of the token is kept
        self.store.delete_session(token)
        self.assertIsNone(self.store.user_for_session(token))

    def test_expired_sessions_are_ignored(self):
        store = AccountStore(self.path, iterations=FAST, session_days=-1, email_checker=accept_all)
        store.migrate()
        user = store.create_user("ay_0b", "ay@example.com", "Tr@deLab1")
        self.assertIsNone(store.user_for_session(store.create_session(user["id"])))

    def test_upgrades_an_earlier_users_table(self):
        # The table an earlier version of TradeLab created, with an existing account.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    display_name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO users (display_name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                ("Ayoub Rannan", "ayoub@example.com", WERKZEUG_PBKDF2, "2026-10-09T20:56:45.897967+00:00"),
            )
        notes = self.store.migrate()
        self.assertEqual(len(notes), 2)
        self.assertIn("Added a username column", notes[0])
        self.assertIn("Added a user_data table", notes[1])
        self.assertIn("username", self.columns())
        # The earlier account is untouched and signs in with its email.
        user = self.store.authenticate("ayoub@example.com", "Tr@deLab1")
        self.assertEqual((user["username"], user["displayName"]), (None, "Ayoub Rannan"))
        # New accounts work alongside it, and its email stays taken.
        self.store.create_user("ay_0b", "new@example.com", "Tr@deLab1")
        with self.assertRaises(AccountError):
            self.store.create_user("someone", "Ayoub@Example.com", "Tr@deLab1")
        self.assertEqual(self.store.count_users(), 2)

    def test_refuses_an_unrecognized_users_table(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        with self.assertRaises(DatabaseSetupError):
            self.store.migrate()

    def test_rejects_an_email_that_cannot_receive_mail(self):
        only_gmail = lambda domain: ("ok", [(5, "gmail-smtp-in.l.google.com")]) if domain == CANARY_DOMAIN else ("nxdomain", [])  # noqa: E731
        store = AccountStore(self.path, iterations=FAST, email_checker=EmailChecker(lookup=only_gmail))
        store.migrate()
        with self.assertRaises(AccountError) as caught:
            store.create_user("ay_0b", "ay@gnail.com", "Tr@deLab1")
        self.assertEqual(caught.exception.fields, {"email": "Did you mean ay@gmail.com? gnail.com looks like a typo."})
        self.assertEqual(caught.exception.suggestions, {"email": "ay@gmail.com"})
        self.assertEqual(caught.exception.to_dict()["details"]["suggestions"], {"email": "ay@gmail.com"})
        with self.assertRaises(AccountError) as caught:
            store.create_user("ay_0b", "ay@no-such-place.com", "Tr@deLab1")
        self.assertIn("doesn't exist", caught.exception.fields["email"])
        self.assertEqual(store.count_users(), 0)


class EmailCheckerTests(unittest.TestCase):
    def checker(self, result=("ok", [(10, "mx.mail.test")]), canary=("ok", [(5, "gmail-smtp-in.l.google.com")])):
        calls = []

        def lookup(domain):
            calls.append(domain)
            return canary if domain == CANARY_DOMAIN else result

        checker = EmailChecker(lookup=lookup)
        checker.calls = calls
        return checker

    def test_typos_of_popular_providers(self):
        cases = {
            "gnail.com": "gmail.com",
            "gmial.com": "gmail.com",
            "gamil.com": "gmail.com",
            "gmail.con": "gmail.com",
            "gmail.co": "gmail.com",
            "hotmial.com": "hotmail.com",
            "hotmaill.com": "hotmail.com",
            "hotmail.cm": "hotmail.com",
            "yahooo.com": "yahoo.com",
            "outlok.com": "outlook.com",
            "outlook.co": "outlook.com",
            "iclould.com": "icloud.com",
        }
        for domain, expected in cases.items():
            self.assertEqual(typo_suggestion(domain), expected, domain)

    def test_real_domains_are_not_typos(self):
        for domain in ["gmail.com", "ymail.com", "mail.com", "email.com", "aol.com", "aim.com", "me.com", "ms.com",
                       "hotmail.co.uk", "outlook.de", "jjay.cuny.edu", "proton.me", "live.com", "cloud.com", "grail.com",
                       # Country domains of the providers, two edits from the .com
                       "hotmail.ca", "hotmail.no", "outlook.cl", "outlook.cz", "protonmail.ch"]:
            self.assertIsNone(typo_suggestion(domain), domain)

    def test_wrong_ending_and_typo_together(self):
        result = self.checker()("a@gmaill.con")
        self.assertEqual((result.status, result.suggestion), ("invalid", "a@gmail.com"))

    def test_typo_is_rejected_before_any_lookup(self):
        checker = self.checker()
        result = checker("Ayoub@Gnail.com")
        self.assertEqual((result.status, result.suggestion), ("invalid", "ayoub@gmail.com"))
        self.assertEqual(checker.calls, [])  # typo-squatted domains often have mail servers, so DNS can't be the judge

    def test_reserved_domains_and_fake_endings(self):
        checker = self.checker()
        self.assertIn("reserved", checker("a@example.com").message)
        self.assertIn("reserved", checker("a@mail.example.org").message)
        self.assertIn("reserved", checker("a@site.test").message)
        fixed = checker("a@jjay.cuny.ed")
        self.assertEqual((fixed.status, fixed.suggestion), ("invalid", "a@jjay.cuny.edu"))
        self.assertEqual(checker("a@shop.con").suggestion, "a@shop.com")
        self.assertEqual(checker.calls, [])

    def test_dns_answers(self):
        self.assertEqual(self.checker(("ok", [(5, "mx.example-mail.net")]))("a@real-domain.com").status, "ok")
        missing = self.checker(("nxdomain", []))("a@made-up-domain.com")
        self.assertEqual(missing.status, "invalid")
        self.assertIn("doesn't exist", missing.message)
        no_mail = self.checker(("ok", []))("a@website-only.com")
        self.assertIn("doesn't receive email", no_mail.message)
        null_mx = self.checker(("ok", [(0, "")]))("a@no-mail-ever.com")
        self.assertIn("doesn't accept email", null_mx.message)

    def test_dns_that_says_nothing_exists_does_not_block_sign_up(self):
        # Some network filters answer "no such domain" for everything, gmail.com included.
        for answer in [("nxdomain", []), ("ok", [])]:
            result = self.checker(answer, canary=answer)("a@real-domain.com")
            self.assertEqual(result.status, "unverified", answer)
            self.assertTrue(result.ok)
        self.assertEqual(self.checker(("nxdomain", []), canary=("nxdomain", []))("a@gmail.com").status, "unverified")

    def test_unreachable_dns_does_not_block_sign_up(self):
        result = self.checker(("unknown", []))("a@real-domain.com")
        self.assertEqual(result.status, "unverified")
        self.assertTrue(result.ok)

    def test_results_are_cached(self):
        checker = self.checker()
        checker("a@real-domain.com")
        checker("b@real-domain.com")
        self.assertEqual(checker.calls, ["real-domain.com"])


class DnsTests(unittest.TestCase):
    def test_query_format(self):
        query = build_query("gmail.com", 0x1234)
        self.assertEqual(query[:2], b"\x12\x34")
        self.assertIn(b"\x05gmail\x03com\x00\x00\x0f\x00\x01", query)  # MX question
        self.assertTrue(query.endswith(b"\x00\x00\x29\x04\xd0\x00\x00\x00\x00\x00\x00"))  # EDNS, 1232 bytes

    def test_parses_real_replies(self):
        rcode, truncated, records = parse_mx_response(DNS_REPLIES["gmail.com"], 0x1234)
        self.assertEqual((rcode, truncated), (0, False))
        self.assertIn((5, "gmail-smtp-in.l.google.com"), records)  # compressed name
        self.assertEqual(len(records), 5)
        self.assertEqual(parse_mx_response(DNS_REPLIES["example.com"], 0x1234), (0, False, [(0, "")]))  # null MX
        self.assertEqual(parse_mx_response(DNS_REPLIES["no-such-domain-4815.com"], 0x1234)[0], 3)  # NXDOMAIN
        self.assertEqual(parse_mx_response(DNS_REPLIES["github.io"], 0x1234), (0, False, []))  # no MX

    def test_rejects_replies_to_other_queries(self):
        with self.assertRaises(ValueError):
            parse_mx_response(DNS_REPLIES["gmail.com"], 0x9999)
        with self.assertRaises(ValueError):
            parse_mx_response(b"\x12\x34\x81", 0x1234)

    def test_lookup_over_udp(self):
        # A stand-in DNS server: answers every query with the canned gmail.com reply.
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]

        def serve():
            query, client = server.recvfrom(512)
            server.sendto(query[:2] + DNS_REPLIES["gmail.com"][2:], client)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            # The first server never answers; the lookup moves on to the second.
            result, records = lookup_mx("gmail.com", ["127.0.0.1:9", f"127.0.0.1:{port}"], timeout=0.5)
        finally:
            thread.join(2)
            server.close()
        self.assertEqual(result, "ok")
        self.assertEqual(len(records), 5)

    def test_lookup_gives_up_without_servers(self):
        self.assertEqual(lookup_mx("gmail.com", ["127.0.0.1:9"], timeout=0.2), ("unknown", []))


class UserDataTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = AccountStore(os.path.join(self.folder.name, "tradelab.db"), iterations=FAST, email_checker=accept_all)
        self.store.migrate()
        self.ana = self.store.create_user("ana", "ana@mail.org", "Tr@deLab1")
        self.ben = self.store.create_user("ben", "ben@mail.org", "Tr@deLab1")

    def tearDown(self):
        self.folder.cleanup()

    def test_each_account_keeps_its_own_progress(self):
        self.assertEqual(self.store.load_user_data(self.ana["id"]), (None, None))
        ana_data = {"schemaVersion": 1, "state": {"learning": {"lessons": {"what-is-a-stock": {"completedAt": 1}}}}}
        ben_data = {"schemaVersion": 1, "state": {"learning": {"lessons": {}}}}
        self.store.save_user_data(self.ana["id"], ana_data)
        self.store.save_user_data(self.ben["id"], ben_data)
        self.assertEqual(self.store.load_user_data(self.ana["id"])[0], ana_data)
        self.assertEqual(self.store.load_user_data(self.ben["id"])[0], ben_data)
        ana_data["state"]["learning"]["lessons"]["how-prices-move"] = {"completedAt": 2}
        self.store.save_user_data(self.ana["id"], ana_data)  # saving again replaces
        self.assertEqual(len(self.store.load_user_data(self.ana["id"])[0]["state"]["learning"]["lessons"]), 2)

    def test_rejects_bad_or_huge_documents(self):
        for bad in [None, [], "text", {"state": []}, {"schemaVersion": 1}]:
            with self.assertRaises(AccountError):
                self.store.save_user_data(self.ana["id"], bad)
        with self.assertRaises(AccountError) as caught:
            self.store.save_user_data(self.ana["id"], {"state": {"blob": "x" * (2 * 1024 * 1024)}})
        self.assertEqual(caught.exception.status, 413)


def load_server_module():
    spec = importlib.util.spec_from_file_location("tradelab_dev_server", os.path.join(ROOT, "scripts", "dev_server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ServerTests(unittest.TestCase):
    """The HTTP API, end to end, on a free local port."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.store = AccountStore(os.path.join(cls.folder.name, "tradelab.db"), iterations=FAST, email_checker=EmailChecker(lookup=lambda domain: ("ok", [(10, "mx." + domain)])))
        cls.store.migrate()
        cls.httpd = load_server_module().make_server(cls.store, 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.folder.cleanup()

    def client(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, opener, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with opener.open(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = None
            return error.code, body

    def test_email_check_endpoint(self):
        opener = self.client()
        self.assertEqual(self.call(opener, "POST", "/api/email-check", {"email": "ay@gmail.com"})[1]["status"], "ok")
        status, typo = self.call(opener, "POST", "/api/email-check", {"email": "ay@gnail.com"})
        self.assertEqual((status, typo["status"], typo["suggestion"]), (200, "invalid", "ay@gmail.com"))
        self.assertEqual(self.call(opener, "POST", "/api/email-check", {"email": "nope"})[1]["status"], "invalid")

    def test_sign_up_rejects_a_typo_with_a_suggestion(self):
        status, body = self.call(self.client(), "POST", "/api/users", {"username": "typo", "email": "typo@gnail.com", "password": "Tr@deLab1"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["suggestions"], {"email": "typo@gmail.com"})

    def test_progress_is_private_to_each_account(self):
        ana, ben, guest = self.client(), self.client(), self.client()
        self.assertEqual(self.call(ana, "POST", "/api/users", {"username": "ana_http", "email": "ana@mail.org", "password": "Tr@deLab1"})[0], 201)
        self.assertEqual(self.call(ben, "POST", "/api/users", {"username": "ben_http", "email": "ben@mail.org", "password": "Tr@deLab1"})[0], 201)
        self.assertEqual(self.call(ana, "GET", "/api/me/data")[1], {"data": None, "updatedAt": None})
        progress = {"schemaVersion": 1, "state": {"learning": {"lessons": {"what-is-a-stock": {"completedAt": 1}}}}}
        status, saved = self.call(ana, "PUT", "/api/me/data", {"data": progress})
        self.assertEqual(status, 200)
        self.assertTrue(saved["updatedAt"])
        self.assertEqual(self.call(ana, "GET", "/api/me/data")[1]["data"], progress)
        self.assertIsNone(self.call(ben, "GET", "/api/me/data")[1]["data"])  # Ben can't see Ana's progress
        status, body = self.call(guest, "GET", "/api/me/data")
        self.assertEqual((status, body["error"]["code"]), (401, "not_signed_in"))
        self.assertEqual(self.call(guest, "PUT", "/api/me/data", {"data": progress})[0], 401)
        # After signing out, the same browser can't read or write the account's progress.
        self.assertEqual(self.call(ana, "DELETE", "/api/session")[0], 204)
        self.assertEqual(self.call(ana, "GET", "/api/me/data")[0], 401)

    def test_guest_limits_and_lesson_content_are_enforced_by_the_standard_server(self):
        guest = self.client()
        session_status, session = self.call(guest, "GET", "/api/session")
        self.assertEqual(session_status, 200)
        self.assertEqual(session["guestLimits"]["simulationDaysRemaining"], 21)
        self.assertEqual(self.call(guest, "GET", "/api/lessons/what-is-a-stock")[0], 200)
        self.assertEqual(self.call(guest, "GET", "/api/lessons/candlestick-anatomy")[0], 403)
        self.assertEqual(self.call(guest, "GET", "/api/lessons/candlestick-anatomy/access")[0], 403)
        self.assertEqual(self.call(guest, "GET", "/server/lesson_content.json")[0], 404)

        for _ in range(4):
            self.assertEqual(self.call(guest, "POST", "/api/guest/simulation/advance", {"days": 5})[0], 200)
        status, final_week = self.call(guest, "POST", "/api/guest/simulation/advance", {"days": 5})
        self.assertEqual((status, final_week["grantedDays"], final_week["limits"]["simulationDay"]), (200, 1, 36))
        self.assertEqual(self.call(guest, "POST", "/api/guest/simulation/advance", {"days": 1})[0], 403)

        for remaining in (1, 0):
            status, result = self.call(guest, "POST", "/api/guest/journal/entries", {})
            self.assertEqual((status, result["limits"]["journalEntriesRemaining"]), (200, remaining))
        self.assertEqual(self.call(guest, "POST", "/api/guest/journal/entries", {})[0], 403)

    def test_standard_server_does_not_apply_guest_quotas_to_signed_in_accounts(self):
        account = self.client()
        status, _user = self.call(
            account,
            "POST",
            "/api/users",
            {"username": "full_access", "email": "full_access@mail.org", "password": "Tr@deLab1"},
        )
        self.assertEqual(status, 201)
        simulation = self.call(account, "POST", "/api/guest/simulation/advance", {"days": 5})
        journal = self.call(account, "POST", "/api/guest/journal/entries", {})
        lesson = self.call(account, "GET", "/api/lessons/candlestick-anatomy")
        self.assertEqual(simulation, (200, {"authenticated": True}))
        self.assertEqual(journal, (200, {"authenticated": True}))
        self.assertEqual(lesson[0], 200)
        self.assertEqual(lesson[1]["lesson"]["id"], "candlestick-anatomy")


if __name__ == "__main__":
    unittest.main()
