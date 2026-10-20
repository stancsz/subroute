"""Read a payment provider CSV export."""

from ledger import LedgerError


def parse_events(csv_text: str) -> list[dict]:
    """Parse and validate rows according to README.md."""
    raise NotImplementedError
