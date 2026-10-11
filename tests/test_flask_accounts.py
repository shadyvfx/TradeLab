"""The account API on the Flask app: the same contract as scripts/dev_server.py."""

import json
import os
import tempfile
import unittest

from backend.app import app
from server.accounts import AccountStore
from server.email_check import EmailCheck, EmailChecker

FAST = 1000  # PBKDF2 iterations; real accounts use 1,000,000
PASSWORD = "Tr@deLab2026"


def accept_all(email):
    return EmailCheck("ok", "ok", None, email.rpartition("@")[2])


class FlaskAccountTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = AccountStore(os.path.join(self.folder.name, "tradelab.db"), iterations=FAST, email_checker=accept_all)
        self.store.migrate()
        app.config["ACCOUNT_STORE"] = self.store
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        app.config.pop("ACCOUNT_STORE", None)
        self.folder.cleanup()

    def sign_up(self, username="ana_b", email="ana@gmail.com", client=None):
        return (client or self.client).post("/api/users", json={"username": username, "email": email, "password": PASSWORD})

    def test_sign_up_signs_in_with_an_http_only_cookie(self):
        response = self.sign_up()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["user"]["username"], "ana_b")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("tradelab_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)  # plain HTTP on this computer
        self.assertEqual(self.client.get("/api/session").get_json()["user"]["email"], "ana@gmail.com")

    def test_cookie_is_secure_over_https(self):
        response = self.client.post(
            "/api/users",
            json={"username": "ana_b", "email": "ana@gmail.com", "password": PASSWORD},
            base_url="https://localhost",
        )
        self.assertIn("Secure", response.headers["Set-Cookie"])

    def test_sign_in_sign_out_and_wrong_password(self):
        self.sign_up()
        self.assertEqual(self.client.delete("/api/session").status_code, 204)
        self.assertIsNone(self.client.get("/api/session").get_json()["user"])
        wrong = self.client.post("/api/session", json={"login": "ana_b", "password": "Wrong@pass1"})
        self.assertEqual((wrong.status_code, wrong.get_json()["error"]["code"]), (401, "invalid_credentials"))
        right = self.client.post("/api/session", json={"login": "ANA@gmail.com", "password": PASSWORD})
        self.assertEqual(right.status_code, 200)
        self.assertEqual(right.get_json()["user"]["username"], "ana_b")

    def test_sign_up_errors_name_the_fields(self):
        response = self.client.post("/api/users", json={"username": "has space", "email": "nope", "password": "short"})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertEqual(error["code"], "validation_failed")
        self.assertEqual(set(error["details"]["fields"]), {"username", "email", "password"})
        self.sign_up()
        taken = self.sign_up(email="other@gmail.com", client=app.test_client())
        self.assertEqual((taken.status_code, taken.get_json()["error"]["field"]), (409, "username"))

    def test_email_typos_are_rejected_with_a_suggestion(self):
        self.store.email_checker = EmailChecker(lookup=lambda domain: ("ok", [(10, "mx." + domain)]))
        check = self.client.post("/api/email-check", json={"email": "ana@gnail.com"}).get_json()
        self.assertEqual((check["status"], check["suggestion"]), ("invalid", "ana@gmail.com"))
        response = self.sign_up(email="ana@gnail.com")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["details"]["suggestions"], {"email": "ana@gmail.com"})

    def test_progress_is_saved_per_account(self):
        self.assertEqual(self.client.get("/api/me/data").status_code, 401)
        self.sign_up()
        self.assertEqual(self.client.get("/api/me/data").get_json(), {"data": None, "updatedAt": None})
        document = {"schemaVersion": 1, "state": {"learning": {"lessons": {"what-is-a-stock": {"completedAt": 1}}}}}
        saved = self.client.put("/api/me/data", json={"data": document})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.client.get("/api/me/data").get_json()["data"], document)

        ben = app.test_client()
        self.sign_up("ben_c", "ben@gmail.com", client=ben)
        self.assertIsNone(ben.get("/api/me/data").get_json()["data"])  # Ana's progress stays Ana's

    def test_large_progress_documents_fit_but_tutor_requests_stay_small(self):
        self.sign_up()
        big = {"schemaVersion": 1, "state": {"tutor": {"messages": ["x" * 1000] * 300}}}  # about 300 KB
        self.assertEqual(self.client.put("/api/me/data", json={"data": big}).status_code, 200)
        tutor = self.client.post("/api/tutor/chat", data=json.dumps({"messages": ["x" * 70000]}), content_type="application/json")
        self.assertEqual((tutor.status_code, tutor.get_json()["error"]["code"]), (413, "request_too_large"))

    def test_other_websites_cannot_change_anything(self):
        response = self.client.post(
            "/api/users",
            json={"username": "ana_b", "email": "ana@gmail.com", "password": PASSWORD},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual((response.status_code, response.get_json()["error"]["code"]), (403, "forbidden_origin"))
        same_site = self.client.post(
            "/api/users",
            json={"username": "ana_b", "email": "ana@gmail.com", "password": PASSWORD},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(same_site.status_code, 201)

    def test_bodies_must_be_json(self):
        response = self.client.post("/api/session", data="login=ana", content_type="application/x-www-form-urlencoded")
        self.assertEqual((response.status_code, response.get_json()["error"]["code"]), (415, "unsupported_media_type"))

    def test_account_requests_stay_small(self):
        padded = {"login": "ana_b", "password": PASSWORD, "padding": "x" * (20 * 1024)}
        response = self.client.post("/api/session", json=padded)
        self.assertEqual((response.status_code, response.get_json()["error"]["code"]), (413, "payload_too_large"))

    def test_wrong_method_answers_in_json(self):
        response = self.client.put("/api/session", json={})
        self.assertEqual((response.status_code, response.get_json()["error"]["code"]), (405, "method_not_allowed"))

    def test_database_and_server_code_are_never_served(self):
        private = [
            "/tradelab.db",
            "/server/accounts.py",
            "/server/lesson_content.json",  # account-only lessons: only GET /api/lessons/<id> hands them out
            "/backend/app.py",
            "/serve.py",
            "/deploy/tradelab.service",
            "/.git/config",
        ]
        for path in private:
            self.assertEqual(self.client.get(path).status_code, 404, path)


if __name__ == "__main__":
    unittest.main()
