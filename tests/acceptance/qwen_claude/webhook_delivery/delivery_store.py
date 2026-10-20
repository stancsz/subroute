"""SQLite-backed idempotency for verified webhook deliveries."""

from webhook import WebhookError


class DeliveryStore:
    def __init__(self, db_path):
        raise NotImplementedError

    def record(self, event):
        raise NotImplementedError

    def get(self, delivery_id):
        raise NotImplementedError

    def count(self):
        raise NotImplementedError
