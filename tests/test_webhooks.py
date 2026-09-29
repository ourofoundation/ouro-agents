"""Webhook delivery dedupe and X-Ouro-Signature verification."""

import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ouro_agents import server
from ouro_agents.config import ServerConfig
from ouro_agents.webhooks import DeliveryDedupe, verify_webhook_signature

SECRET = "whsec_" + "cd" * 24


def _sign(body: bytes, t: int | None = None, secret: str = SECRET) -> str:
    t = int(time.time()) if t is None else t
    digest = hmac.new(
        secret.encode(), f"{t}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={t},v1={digest}"


def _body(delivery_id: str | None = "delivery-1", event: str = "comment") -> bytes:
    payload = {"event": event, "data": {}, "timestamp": "2026-09-29T00:00:00Z"}
    if delivery_id is not None:
        payload["delivery_id"] = delivery_id
    # Compact separators, like the backend's JSON.stringify.
    return json.dumps(payload, separators=(",", ":")).encode()


class DeliveryDedupeTests(unittest.TestCase):
    def test_claim_is_check_and_insert(self):
        dedupe = DeliveryDedupe()
        self.assertTrue(dedupe.claim("a"))
        self.assertFalse(dedupe.claim("a"))
        self.assertTrue(dedupe.claim("b"))

    def test_entries_expire_after_ttl(self):
        now = [1000.0]
        dedupe = DeliveryDedupe(ttl_seconds=60, clock=lambda: now[0])
        self.assertTrue(dedupe.claim("a"))
        now[0] += 59
        self.assertFalse(dedupe.claim("a"))
        now[0] += 2
        self.assertTrue(dedupe.claim("a"))

    def test_bounded_size_evicts_oldest(self):
        dedupe = DeliveryDedupe(max_entries=2)
        for delivery_id in ("a", "b", "c"):
            dedupe.claim(delivery_id)
        self.assertEqual(len(dedupe), 2)
        self.assertNotIn("a", dedupe)
        self.assertIn("c", dedupe)

    def test_release_allows_reclaim(self):
        dedupe = DeliveryDedupe()
        dedupe.claim("a")
        dedupe.release("a")
        self.assertTrue(dedupe.claim("a"))


class VerifySignatureTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        body = _body()
        t = 1_790_000_000
        header = _sign(body, t)
        self.assertTrue(verify_webhook_signature(body, header, SECRET, now=t + 299))
        self.assertFalse(verify_webhook_signature(body, header, SECRET, now=t + 301))
        self.assertFalse(verify_webhook_signature(body + b"x", header, SECRET, now=t))
        self.assertFalse(verify_webhook_signature(body, header, "whsec_x", now=t))
        self.assertFalse(verify_webhook_signature(body, None, SECRET, now=t))


class ServerConfigTests(unittest.TestCase):
    def test_webhook_secret_defaults_off_and_blank_is_unset(self):
        self.assertIsNone(ServerConfig().webhook_secret)
        # "${OURO_WEBHOOK_SECRET}" with the env var unset expands to "".
        self.assertIsNone(ServerConfig(webhook_secret="").webhook_secret)
        self.assertEqual(ServerConfig(webhook_secret=SECRET).webhook_secret, SECRET)
        self.assertEqual(ServerConfig().webhook_dedupe_ttl_seconds, 86400)


class HandleEventRouteTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_api_route("/events", server.handle_event, methods=["POST"])
        self.client = TestClient(app)

        self.fake_agent = MagicMock()
        self.fake_agent.config.server = ServerConfig()
        self.process = AsyncMock(
            return_value={"status": "accepted", "event_type": "comment", "pooled": False}
        )
        patches = [
            patch.object(server, "agent_instance", self.fake_agent),
            patch.object(server, "delivery_dedupe", DeliveryDedupe()),
            patch.object(server, "process_event", self.process),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, body: bytes, headers: dict | None = None):
        return self.client.post(
            "/events",
            content=body,
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    # -- dedupe -------------------------------------------------------------

    def test_retry_with_same_delivery_id_is_acknowledged_without_rerun(self):
        first = self.post(_body("d-1"), {"X-Ouro-Attempt": "1"})
        retry = self.post(_body("d-1"), {"X-Ouro-Attempt": "2"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json().get("pooled"), False)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(
            retry.json(),
            {"status": "accepted", "event_type": "comment", "duplicate": True},
        )
        self.process.assert_awaited_once()

    def test_distinct_delivery_ids_both_run(self):
        self.post(_body("d-1"))
        self.post(_body("d-2"))
        self.assertEqual(self.process.await_count, 2)

    def test_delivery_header_used_when_payload_lacks_id(self):
        self.post(_body(None), {"X-Ouro-Delivery": "hdr-1"})
        dup = self.post(_body(None), {"X-Ouro-Delivery": "hdr-1"})
        self.assertTrue(dup.json().get("duplicate"))
        self.process.assert_awaited_once()

    def test_events_without_delivery_id_are_not_deduped(self):
        self.post(_body(None))
        second = self.post(_body(None))
        self.assertNotIn("duplicate", second.json())
        self.assertEqual(self.process.await_count, 2)

    def test_failed_processing_releases_delivery_for_retry(self):
        self.process.side_effect = [
            HTTPException(status_code=400, detail="bad"),
            {"status": "accepted", "event_type": "comment", "pooled": False},
        ]
        first = self.post(_body("d-1"))
        retry = self.post(_body("d-1"))
        self.assertEqual(first.status_code, 400)
        self.assertEqual(retry.status_code, 200)
        self.assertNotIn("duplicate", retry.json())
        self.assertEqual(self.process.await_count, 2)

    def test_forwards_parsed_body_to_process_event(self):
        self.post(_body("d-1"))
        body_arg = self.process.await_args.args[0]
        self.assertEqual(body_arg["delivery_id"], "d-1")
        self.assertEqual(body_arg["event"], "comment")

    def test_invalid_json_is_400(self):
        self.assertEqual(self.post(b"{not json").status_code, 400)
        self.assertEqual(self.post(b"[1,2]").status_code, 400)
        self.process.assert_not_awaited()

    def test_agent_not_initialized_is_503_and_does_not_claim(self):
        with patch.object(server, "agent_instance", None):
            self.assertEqual(self.post(_body("d-1")).status_code, 503)
        self.assertEqual(self.post(_body("d-1")).status_code, 200)
        self.process.assert_awaited_once()

    # -- signature ----------------------------------------------------------

    def test_no_secret_configured_skips_verification(self):
        response = self.post(_body(), {"X-Ouro-Signature": "t=1,v1=bogus"})
        self.assertEqual(response.status_code, 200)

    def test_valid_signature_accepted(self):
        self.fake_agent.config.server = ServerConfig(webhook_secret=SECRET)
        body = _body()
        response = self.post(body, {"X-Ouro-Signature": _sign(body)})
        self.assertEqual(response.status_code, 200)
        self.process.assert_awaited_once()

    def test_signature_is_checked_against_raw_bytes(self):
        # Whitespace that a parse/re-serialize round trip would drop.
        self.fake_agent.config.server = ServerConfig(webhook_secret=SECRET)
        body = b'{ "event": "comment", "delivery_id": "d-9", "data": {} }'
        response = self.post(body, {"X-Ouro-Signature": _sign(body)})
        self.assertEqual(response.status_code, 200)

    def test_bad_missing_or_stale_signature_is_401(self):
        self.fake_agent.config.server = ServerConfig(webhook_secret=SECRET)
        body = _body()
        cases = {
            "missing": {},
            "wrong secret": {"X-Ouro-Signature": _sign(body, secret="whsec_other")},
            "tampered": {"X-Ouro-Signature": _sign(body + b" ")},
            "stale": {"X-Ouro-Signature": _sign(body, int(time.time()) - 301)},
            "future": {"X-Ouro-Signature": _sign(body, int(time.time()) + 301)},
            "malformed": {"X-Ouro-Signature": "v1=abc"},
        }
        for name, headers in cases.items():
            with self.subTest(name):
                self.assertEqual(self.post(body, headers).status_code, 401)
        self.process.assert_not_awaited()

    def test_rejected_signature_does_not_claim_delivery(self):
        self.fake_agent.config.server = ServerConfig(webhook_secret=SECRET)
        body = _body("d-1")
        self.assertEqual(
            self.post(body, {"X-Ouro-Signature": "t=1,v1=bad"}).status_code, 401
        )
        ok = self.post(body, {"X-Ouro-Signature": _sign(body)})
        self.assertEqual(ok.status_code, 200)
        self.assertNotIn("duplicate", ok.json())


if __name__ == "__main__":
    unittest.main()
