#!/usr/bin/env python3
"""PostgreSQL contract/concurrency tests; uses an isolated schema, no real payments.
Run on the remote debug host with the existing PostgreSQL test container.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import uuid

schema = 'quota_test_' + uuid.uuid4().hex
command = ['docker', 'exec', '-i', 'hyperswitch-debug-pg', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', 'db_user', '-d', 'hyperswitch_db']
env = dict(os.environ, DOCKER_HOST='unix:///run/user/1000/docker.sock')

def sql(text):
    r = subprocess.run(command, input=f'SET search_path TO {schema};\n' + text, text=True, capture_output=True, env=env)
    if r.returncode:
        raise AssertionError(r.stderr)
    return r.stdout.strip()

def call(operation, **data):
    encoded = json.dumps(data).replace("'", "''")
    return json.loads(sql(f"SELECT channel_limit_command('{operation}','{encoded}'::jsonb);"))

checks = []
def check(label, value):
    assert value, label
    checks.append(label)

try:
    sql(f'CREATE SCHEMA {schema}; SET search_path TO {schema}; CREATE TABLE merchant_connector_account (merchant_id TEXT,merchant_connector_id TEXT); INSERT INTO merchant_connector_account VALUES (\'a\',\'a1\'),(\'b\',\'b1\');')
    sql(Path(__file__).resolve().parents[1].joinpath('migrations/2026-10-20-000001_channel_daily_limits/up.sql').read_text())
    policy = dict(merchant_id='a', merchant_connector_id='a1', pool_id='shared', currency='USD', timezone='Asia/Shanghai', daily_limit=1000)
    call('set', **policy)
    call('set', **dict(policy, merchant_id='b', merchant_connector_id='b1'))
    def reserve(i):
        return call('reserve', merchant_id='a' if i % 2 else 'b', merchant_connector_id='a1' if i % 2 else 'b1', attempt_id=f'try{i}', currency='USD', amount=100)['allowed']
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        allowed = list(executor.map(reserve, range(30)))
    check('concurrent shared pool never exceeds cap', sum(allowed) == 10)
    check('usage reflects shared merchant holds', all(row['used'] == 1000 for row in call('list')))
    selected = next(i for i, ok in enumerate(allowed) if ok)
    base = dict(merchant_id='a' if selected % 2 else 'b', merchant_connector_id='a1' if selected % 2 else 'b1', attempt_id=f'try{selected}', currency='USD', amount=100)
    check('same attempt reservation idempotent', call('reserve', **base)['allowed'])
    check('increased amount cannot reuse reservation', not call('reserve', **dict(base, amount=101))['allowed'])
    check('unconfigured currency cannot bypass managed channel', not call('reserve', **dict(base, currency='EUR'))['allowed'])
    call('settle', **base, state='released')
    check('released attempt cannot submit again', not call('reserve', **base)['allowed'])
    check('definite failure releases quota', call('reserve', **dict(base, attempt_id='after_failure'))['allowed'])
    call('settle', **dict(base, attempt_id='after_failure'), state='committed')
    call('settle', **dict(base, attempt_id='after_failure'), state='released')
    check('refund cannot restore committed quota', not call('reserve', **dict(base, attempt_id='after_refund'))['allowed'])
    call('set', **dict(policy, enabled=False))
    check('disabled channel rejects existing attempt', not call('reserve', **dict(base, attempt_id='after_failure'))['allowed'])
    call('set', **dict(policy, enabled=True, daily_limit=900))
    check('lowering limit preserves prior usage', not call('check', **dict(base, attempt_id='after_lower'))['allowed'])
    check('unmanaged channel remains unchanged', call('reserve', merchant_id='a',merchant_connector_id='other',attempt_id='x',currency='USD',amount=1)['allowed'])
    for label, change in [('timezone immutable', dict(timezone='UTC')), ('binding immutable', dict(pool_id='other'))]:
        try:
            call('set', **dict(policy, **change))
        except AssertionError:
            checks.append(label)
        else:
            raise AssertionError(label)
    try:
        call('resolve', **base, state='released', reason='')
    except AssertionError:
        checks.append('manual resolution requires reconciliation evidence')
    else:
        raise AssertionError('resolution accepted without evidence')
    call('resolve', **dict(base, attempt_id='after_failure'), state='released', reason='Reconciled with upstream: already charged')
    check('manual resolution cannot release committed payment', not call('reserve', **dict(base, attempt_id='manual_probe'))['allowed'])
    # Old buckets remain durable, while a new local day starts with a new allowance.
    sql("UPDATE channel_limit_reservation SET bucket=bucket-1;")
    call('set', **policy)
    check('new day carries unresolved holds forward', not call('reserve', **dict(base, attempt_id='large_new_day', amount=1000))['allowed'])
    check('new local day excludes previous committed amount', call('reserve', **dict(base, attempt_id='new_day'))['allowed'])
    check('previous-day committed attempt cannot initiate another charge', not call('reserve', **dict(base, attempt_id='after_failure'))['allowed'])
    call('settle', **dict(base, attempt_id='new_day'), state='committed')
    check('new day commit preserves total occupied capacity', all(row['used']==1000 for row in call('list')))
    print(json.dumps({'passed': len(checks), 'checks': checks}, indent=2))
finally:
    sql(f'DROP SCHEMA IF EXISTS {schema} CASCADE;')
