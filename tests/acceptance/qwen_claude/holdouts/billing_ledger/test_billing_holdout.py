import unittest

from csv_ingest import parse_events
from ledger import LedgerError, reconcile


class Holdout(unittest.TestCase):
    def test_bom_quoted_id_and_blank_line(self):
        text = '\ufeffevent_id,account_id,kind,amount_cents,charge_id\n"c,1","shop,west",charge,1000,\n\n"r,1","shop,west",refund,250,"c,1"\n'
        self.assertEqual(reconcile(parse_events(text)), {'shop,west': 750})

    def test_bool_amount_is_not_an_integer_event(self):
        event = {'event_id': 'c1', 'account_id': 'a', 'kind': 'charge', 'amount_cents': True, 'charge_id': ''}
        with self.assertRaises(LedgerError):
            reconcile([event])

    def test_cumulative_refund_and_cross_account_replay(self):
        c = {'event_id': 'c', 'account_id': 'a', 'kind': 'charge', 'amount_cents': 100, 'charge_id': ''}
        r1 = {'event_id': 'r1', 'account_id': 'a', 'kind': 'refund', 'amount_cents': 60, 'charge_id': 'c'}
        r2 = {'event_id': 'r2', 'account_id': 'a', 'kind': 'refund', 'amount_cents': 41, 'charge_id': 'c'}
        with self.assertRaises(LedgerError):
            reconcile([c, r1, r2])
        with self.assertRaises(LedgerError):
            reconcile([c, {**c, 'account_id': 'b'}])


if __name__ == '__main__':
    unittest.main()
