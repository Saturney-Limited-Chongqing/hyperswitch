import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reconciliation import statement


class StatementTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE orders(pid TEXT,out_trade_no TEXT,trade_no TEXT,params TEXT,currency TEXT,status TEXT,paid_at INTEGER)')
        self.db.execute('CREATE TABLE refunds(pid TEXT,out_refund_no TEXT,refund_no TEXT,trade_no TEXT,amount INTEGER,currency TEXT,status TEXT,confirmed_at INTEGER)')

    def tearDown(self):
        self.db.close()

    def test_confirmed_events_only_and_merchant_boundary(self):
        for pid, status, paid_at in [('1','paid',100),('1','pending',None),('2','paid',101)]:
            self.db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?)',
                            (pid, 'order'+str(paid_at), 'pay'+str(paid_at),
                             json.dumps({'money':'10.00'}), 'USD', status, paid_at))
        self.db.execute('INSERT INTO refunds VALUES (?,?,?,?,?,?,?,?)',
                        ('1','r1','ref1','pay100',300,'USD','success',120))
        self.db.execute('INSERT INTO refunds VALUES (?,?,?,?,?,?,?,?)',
                        ('1','r2','ref2','pay100',200,'USD','unknown',121))
        result = statement(self.db,'1',90,130)
        self.assertEqual((result['charge_minor'],result['refund_minor'],result['net_minor']), (1000,300,700))
        self.assertEqual(len(result['events']),2)
        self.assertEqual(result['sha256'],statement(self.db,'1',90,130)['sha256'])

    def test_legacy_unverified_currency_fails_closed(self):
        self.db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?)',
                        ('1','old','pay_old',json.dumps({'money':'1.00'}),None,'paid',100))
        with self.assertRaises(ValueError):
            statement(self.db,'1',90,130)

    def test_external_settlement_record_is_immutable_and_cannot_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ledger.db'
            target = sqlite3.connect(path)
            self.db.backup(target)
            target.close()
            path.chmod(0o600)
            base = [sys.executable, str(Path(__file__).with_name('reconciliation.py')),
                    '--database', str(path), '--pid', '1', '--from', '2020-01-01T00:00:00Z',
                    '--to', '2020-01-02T00:00:00Z', '--currency', 'USD']
            args = base + ['record', '--external-reference', 'bank-001',
                           '--evidence', 'Bank statement reference bank-001']
            first = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = subprocess.run(args, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            with sqlite3.connect(path) as audit:
                self.assertEqual(audit.execute('SELECT count(*) FROM external_settlement_records').fetchone()[0], 1)
                with self.assertRaises(sqlite3.IntegrityError):
                    audit.execute('DELETE FROM external_settlement_records')


if __name__ == '__main__':
    unittest.main()
