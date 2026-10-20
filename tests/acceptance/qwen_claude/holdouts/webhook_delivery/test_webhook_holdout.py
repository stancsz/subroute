import concurrent.futures
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from delivery_store import DeliveryStore
from webhook import WebhookError, verify_webhook


class Holdout(unittest.TestCase):
    def test_rejects_nested_duplicate_keys_and_bad_signature_shape(self):
        secret = b"s"
        body = b'{"delivery_id":"d","resource_id":"r","event_type":"updated","revision":1,"meta":{"a":1,"a":2}}'
        digest = hmac.new(secret, b"10." + body, hashlib.sha256).hexdigest()
        with self.assertRaises(WebhookError):
            verify_webhook(body, "sha256=" + digest, "10", secret, 11)
        clean = b'{"delivery_id":"d","resource_id":"r","event_type":"updated","revision":1}'
        signature = "sha256=" + hmac.new(secret, b"10." + clean, hashlib.sha256).hexdigest()
        for value in (" " + signature, signature + " ", signature[:-1], "SHA256=" + signature[7:]):
            with self.subTest(signature=value), self.assertRaises(WebhookError):
                verify_webhook(clean, value, "10", secret, 11)

    def test_rejects_whitespace_ids_and_bool_options(self):
        secret = b"s"
        body = json.dumps({"delivery_id": " d", "resource_id": "r",
                           "event_type": "created", "revision": 1}).encode()
        signature = "sha256=" + hmac.new(secret, b"10." + body, hashlib.sha256).hexdigest()
        with self.assertRaises(WebhookError):
            verify_webhook(body, signature, "10", secret, 11)
        for now, age in ((True, 300), (11, True), (11, 0)):
            with self.subTest(now=now, age=age), self.assertRaises(WebhookError):
                verify_webhook(body, signature, "10", secret, now, age)

    def test_concurrent_replay_across_store_instances(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "replay.sqlite"
            event = {"delivery_id": "delivery-7", "resource_id": "item-8",
                     "event_type": "updated", "revision": 2, "extra": [1, 2, 3]}
            stores = [DeliveryStore(path) for _ in range(8)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda store: store.record(event), stores))
            self.assertEqual(results.count(True), 1)
            self.assertEqual(results.count(False), 7)
            self.assertEqual(DeliveryStore(path).count(), 1)


if __name__ == "__main__":
    unittest.main()
