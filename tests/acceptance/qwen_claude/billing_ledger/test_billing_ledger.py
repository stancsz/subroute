import copy
import unittest

from csv_ingest import parse_events
from ledger import LedgerError, reconcile


class BillingLedgerTests(unittest.TestCase):
    def test_csv_to_balances_with_refunds_and_replay(self):
        text = ('event_id,account_id,kind,amount_cents,charge_id\n'
                'c1,alpha,charge,2500,\n'
                'c2,beta,charge,700,\n'
                'r1,alpha,refund,800,c1\n'
                'r1,alpha,refund,800,c1\n'
                'r2,alpha,refund,200,c1\n')
        events = parse_events(text)
        self.assertEqual(len(events), 5)
        self.assertEqual(events[0]['amount_cents'], 2500)
        before = copy.deepcopy(events)
        self.assertEqual(reconcile(events), {'alpha': 1500, 'beta': 700})
        self.assertEqual(events, before)

    def test_conflicting_replay_and_excess_refund_fail(self):
        base = [{'event_id': 'c1', 'account_id': 'a', 'kind': 'charge', 'amount_cents': 100, 'charge_id': ''}]
        with self.assertRaises(LedgerError):
            reconcile(base + [{**base[0], 'amount_cents': 200}])
        with self.assertRaises(LedgerError):
            reconcile(base + [{'event_id': 'r1', 'account_id': 'a', 'kind': 'refund', 'amount_cents': 101, 'charge_id': 'c1'}])

    def test_csv_rejects_bad_header_and_amount(self):
        with self.assertRaises(LedgerError):
            parse_events('id,account_id,kind,amount_cents,charge_id\nx,a,charge,100,\n')
        with self.assertRaisesRegex(LedgerError, '2'):
            parse_events('event_id,account_id,kind,amount_cents,charge_id\nc1,a,charge,10.00,\n')

    def test_missing_reference_or_account_mismatch_fails(self):
        charge = {'event_id': 'c1', 'account_id': 'a', 'kind': 'charge', 'amount_cents': 100, 'charge_id': ''}
        refund = {'event_id': 'r1', 'account_id': 'b', 'kind': 'refund', 'amount_cents': 20, 'charge_id': 'c1'}
        with self.assertRaises(LedgerError):
            reconcile([charge, refund])
        with self.assertRaises(LedgerError):
            reconcile([{**refund, 'account_id': 'a'}])


if __name__ == '__main__':
    unittest.main()
