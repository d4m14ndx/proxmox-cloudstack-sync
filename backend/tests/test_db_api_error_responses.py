import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as app_main


def asgi_post(path, body=None, token=None):
    """Issue a real POST through the ASGI router without an HTTP test dependency."""
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"x-api-key", token.encode()))
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("test", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app_main.app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], response_body.decode()


class PermanentlyDisabledLegacyWriteTests(unittest.TestCase):
    def setUp(self):
        self.original_engine = app_main.engine
        self.original_token = app_main.settings.api_auth_token
        self.fake_engine = Mock()
        self.fake_engine.cs_db = Mock()
        app_main.engine = self.fake_engine
        app_main.settings.api_auth_token = "review-operator-token-123456"

    def tearDown(self):
        app_main.settings.api_auth_token = self.original_token
        app_main.engine = self.original_engine

    def test_registration_is_gone_and_cannot_call_database(self):
        request = app_main.RegisterRequest(
            proxmox_id="p2:100",
            service_offering_id=1,
            account_id=1,
            domain_id=1,
        )
        with self.assertRaises(HTTPException) as caught:
            app_main.register_vm(request)

        self.assertEqual(410, caught.exception.status_code)
        self.assertIn("removed", caught.exception.detail)
        self.fake_engine.cs_db.register_existing_vm.assert_not_called()

    def test_generic_repair_is_gone_and_cannot_call_database(self):
        with self.assertRaises(HTTPException) as caught:
            app_main.removed_generic_repair("vm-uuid")

        self.assertEqual(410, caught.exception.status_code)
        self.assertIn("removed", caught.exception.detail)
        self.fake_engine.cs_db.repair_registered_vm.assert_not_called()

    def test_http_tombstones_require_auth_then_return_gone(self):
        registration_body = {
            "proxmox_id": "p2:100",
            "service_offering_id": 1,
            "account_id": 1,
            "domain_id": 1,
        }

        self.assertEqual(401, asgi_post("/api/register", registration_body)[0])
        self.assertEqual(
            401, asgi_post("/api/cloudstack/repair-vm/review-cs")[0]
        )

        token = app_main.settings.api_auth_token
        registration = asgi_post("/api/register", registration_body, token)
        repair = asgi_post("/api/cloudstack/repair-vm/review-cs", token=token)
        self.assertEqual(410, registration[0], registration[1])
        self.assertEqual(410, repair[0], repair[1])
        self.fake_engine.cs_db.register_existing_vm.assert_not_called()
        self.fake_engine.cs_db.repair_registered_vm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
