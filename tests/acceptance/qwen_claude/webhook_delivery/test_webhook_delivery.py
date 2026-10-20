import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from delivery_store import DeliveryStore
from webhook import WebhookError, verify_webhook


SECRET = b"provider-secret"


def signed(body, timestamp="1700000000"):
    digest = hmac.new(SECRET, timestamp.encode("ascii") + b"." + body,
                      hashlib.sha256).hexdigest()
    return "sha256=" + digest


class WebhookTests(unittest.TestCase):
    def test_valid_request_and_age_boundary(self):
        body = json.dumps({"delivery_id": "d-1", "resource_id": "r-2",
                           "event_type": "created", "revision": 0,
                           "details": {"note": "é"}}).encode("utf-8")
        self.assertEqual(verify_webhook(body, signed(body), "1700000000",
                                        SECRET, 1700000300)["details"], {"note": "é"})

    def test_raw_body_signature_and_bad_time(self):
        body = b'{"delivery_id":"d","resource_id":"r","event_type":"deleted","revision":2}'
        with self.assertRaises(WebhookError):
            verify_webhook(body + b" ", signed(body), "1700000000", SECRET, 1700000001)
        with self.assertRaises(WebhookError):
            verify_webhook(body, signed(body), "1700000000", SECRET, 1699999999)
        with self.assertRaises(WebhookError):
            verify_webhook(body, signed(body), "1700000000", SECRET, 1700000301)

    def test_invalid_payload_and_duplicate_keys(self):
        for body in (
            b'{"delivery_id":"d","delivery_id":"e","resource_id":"r","event_type":"created","revision":0}',
            b'{"delivery_id":"d","resource_id":"r","event_type":"created","revision":true}',
            b'{"delivery_id":"d","resource_id":"r","event_type":"created","revision":NaN}',
        ):
            with self.subTest(body=body), self.assertRaises(WebhookError):
                verify_webhook(body, signed(body), "1700000000", SECRET, 1700000001)


class StoreTests(unittest.TestCase):
    def test_persistent_idempotency_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deliveries.sqlite"
            event = {"delivery_id": "d", "resource_id": "r",
                     "event_type": "created", "revision": 1,
                     "details": {"x": 1, "y": 2}}
            store = DeliveryStore(path)
            self.assertTrue(store.record(event))
            event["details"]["x"] = 999
            other = DeliveryStore(path)
            self.assertEqual(other.get("d")["details"], {"x": 1, "y": 2})
            self.assertFalse(other.record({"details": {"y": 2, "x": 1},
                                           "revision": 1, "event_type": "created",
                                           "resource_id": "r", "delivery_id": "d"}))
            with self.assertRaises(WebhookError):
                other.record(event)
            self.assertEqual(store.count(), 1)

    def test_rejects_bad_direct_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(Path(directory) / "deliveries.sqlite")
            with self.assertRaises(WebhookError):
                store.record({"delivery_id": "d", "resource_id": "r",
                              "event_type": "created", "revision": True})


if __name__ == "__main__":
    unittest.main()
