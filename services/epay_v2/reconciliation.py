#!/usr/bin/env python3
"""Offline, read-only-in-effect reconciliation and external settlement register.

The register records evidence of an operator-confirmed external transfer. It never
initiates a payout or represents money held by this gateway.
"""
import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def epoch(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Use a timezone-aware ISO 8601 timestamp')
    return int(parsed.timestamp())


def statement(db, pid, start, end):
    if db.execute("SELECT 1 FROM refunds WHERE pid=? AND status='success' AND confirmed_at IS NULL LIMIT 1", (pid,)).fetchone():
        raise ValueError('Historical successful refund lacks a verified confirmation time')
    orders = db.execute(
        "SELECT out_trade_no,trade_no,params,currency,paid_at FROM orders WHERE pid=? AND status='paid' AND paid_at>=? AND paid_at<? ORDER BY paid_at,trade_no",
        (pid, start, end),
    ).fetchall()
    refunds = db.execute(
        "SELECT out_refund_no,refund_no,trade_no,amount,currency,confirmed_at FROM refunds WHERE pid=? AND status='success' AND confirmed_at>=? AND confirmed_at<? ORDER BY confirmed_at,refund_no",
        (pid, start, end),
    ).fetchall()
    events = []
    for row in orders:
        if row['currency'] is None:
            raise ValueError('Historical order has no verified currency; reconcile it outside this tool')
        params = json.loads(row['params'])
        # Decimal avoids binary floating point and accepts the same two-decimal input as the gateway.
        from decimal import Decimal
        amount = int(Decimal(params['money']) * 100)
        events.append({'kind': 'payment', 'id': row['trade_no'], 'external_id': row['out_trade_no'],
                       'amount_minor': amount, 'currency': row['currency'], 'occurred_at': row['paid_at']})
    for row in refunds:
        events.append({'kind': 'refund', 'id': row['refund_no'], 'external_id': row['out_refund_no'],
                       'payment_id': row['trade_no'], 'amount_minor': -row['amount'], 'currency': row['currency'],
                       'occurred_at': row['confirmed_at']})
    events.sort(key=lambda item: (item['occurred_at'], item['kind'], item['id']))
    charges = sum(item['amount_minor'] for item in events if item['kind'] == 'payment')
    returned = -sum(item['amount_minor'] for item in events if item['kind'] == 'refund')
    manifest = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'pid': pid, 'start': start, 'end': end, 'charge_minor': charges,
            'refund_minor': returned, 'net_minor': charges-returned, 'events': events,
            'sha256': hashlib.sha256(manifest.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description='Offline EPay RSA gateway statement and external settlement register')
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--pid', required=True)
    parser.add_argument('--from', dest='start', required=True, type=epoch)
    parser.add_argument('--to', dest='end', required=True, type=epoch)
    parser.add_argument('--currency', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('statement')
    record = sub.add_parser('record')
    record.add_argument('--external-reference', required=True)
    record.add_argument('--evidence', required=True, help='Bank or processor reconciliation evidence; do not put secrets here')
    args = parser.parse_args()
    if not args.start < args.end <= int(time.time()) or args.end - args.start > 366 * 86400:
        parser.error('Invalid or future statement interval')
    if len(args.evidence if args.command == 'record' else '') > 2000:
        parser.error('Evidence too long')
    if args.database.stat().st_mode & 0o077:
        parser.error('Ledger must be private (0600)')
    db = sqlite3.connect(args.database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            if args.command == 'record':
                db.execute('''CREATE TABLE IF NOT EXISTS external_settlement_records (
                    id INTEGER PRIMARY KEY, pid TEXT NOT NULL, currency TEXT NOT NULL,
                    start_at INTEGER NOT NULL, end_at INTEGER NOT NULL,
                    charge_minor INTEGER NOT NULL, refund_minor INTEGER NOT NULL, net_minor INTEGER NOT NULL,
                    manifest_sha256 TEXT NOT NULL, external_reference TEXT NOT NULL UNIQUE,
                    evidence TEXT NOT NULL, recorded_at INTEGER NOT NULL,
                    UNIQUE(pid,start_at,end_at))''')
                db.execute('''CREATE TRIGGER IF NOT EXISTS settlement_no_update BEFORE UPDATE ON external_settlement_records
                    BEGIN SELECT RAISE(ABORT,'settlement records are immutable'); END''')
                db.execute('''CREATE TRIGGER IF NOT EXISTS settlement_no_delete BEFORE DELETE ON external_settlement_records
                    BEGIN SELECT RAISE(ABORT,'settlement records are immutable'); END''')
                if db.execute('SELECT 1 FROM external_settlement_records WHERE pid=? AND start_at<? AND end_at>?',
                              (args.pid, args.end, args.start)).fetchone():
                    parser.error('Overlapping settlement period already recorded')
            result = statement(db, args.pid, args.start, args.end)
            if any(event['currency'] != args.currency for event in result['events']):
                parser.error('Statement contains another currency')
            result['currency'] = args.currency
            if args.command == 'record':
                if not args.external_reference.strip() or len(args.evidence.strip()) < 10:
                    parser.error('External reference and meaningful evidence required')
                db.execute('''INSERT INTO external_settlement_records
                    (pid,currency,start_at,end_at,charge_minor,refund_minor,net_minor,manifest_sha256,external_reference,evidence,recorded_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (args.pid,args.currency,args.start,args.end,result['charge_minor'],result['refund_minor'],
                     result['net_minor'],result['sha256'],args.external_reference,args.evidence,int(time.time())))
                result['external_reference'] = args.external_reference
                result['recorded'] = True
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        db.close()


if __name__ == '__main__':
    main()
