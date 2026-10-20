"""Validate signed provider webhooks before passing them to the replay ledger."""


class WebhookError(ValueError):
    pass


def verify_webhook(body, signature, timestamp, secret, now, max_age_seconds=300):
    raise NotImplementedError
