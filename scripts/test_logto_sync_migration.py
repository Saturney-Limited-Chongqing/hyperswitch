#!/usr/bin/env python3
"""Run the Logto outbox migration in an isolated PostgreSQL schema (remote debug DB)."""
import json
import os
from pathlib import Path
import subprocess
import uuid

from logto_account_sync import Database


schema = 'logto_sync_test_' + uuid.uuid4().hex
command = ['docker', 'exec', '-i', 'hyperswitch-debug-pg', 'psql', '-X', '-qAt',
           '-v', 'ON_ERROR_STOP=1', '-U', 'db_user', '-d', 'hyperswitch_db']
env = dict(os.environ, DOCKER_HOST='unix:///run/user/1000/docker.sock')


def sql(source):
    result = subprocess.run(command, input=f'SET search_path TO {schema};\n' + source,
                            text=True, capture_output=True, env=env)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


try:
    sql(f'CREATE SCHEMA {schema}; SET search_path TO {schema}; '
        'CREATE TABLE users(user_id VARCHAR(64) UNIQUE, email TEXT, name TEXT, '
        'is_verified BOOLEAN DEFAULT FALSE, is_active BOOLEAN DEFAULT TRUE); '
        "CREATE TYPE \"UserStatus\" AS ENUM ('active','invitation_sent'); "
        'CREATE TABLE user_roles(id SERIAL PRIMARY KEY,user_id VARCHAR(64),org_id VARCHAR(64),'
        'merchant_id VARCHAR(64),role_id VARCHAR(64),status "UserStatus"); '
        "INSERT INTO users VALUES ('u1','u1@example.com','User One');")
    migration = Path(__file__).resolve().parents[1] / 'migrations/2026-10-21-000001_logto_account_sync/up.sql'
    sql(migration.read_text())
    status_migration = Path(__file__).resolve().parents[1] / 'migrations/2026-10-25-000001_logto_user_status_sync/up.sql'
    sql(status_migration.read_text())
    sql("INSERT INTO user_roles(user_id,org_id,merchant_id,role_id,status) VALUES "
        "('u1','o1','m1','collection_merchant_admin','active');")
    first = json.loads(sql('SELECT row_to_json(o) FROM logto_sync_outbox o;'))
    assert (first['generation'], first['state'], first['local_merchant_id']) == (1, 'pending', 'm1')
    sql("UPDATE users SET is_verified=TRUE WHERE user_id='u1';")
    assert sql('SELECT generation FROM logto_sync_outbox;') == '2'
    sql("INSERT INTO logto_identity_binding(local_user_id,local_org_id,local_merchant_id,"
        "logto_subject,logto_organization_id,role_id) VALUES "
        "('u1','o1','m1','logto_u1','logto_o1','collection_merchant_admin');")
    sql("UPDATE user_roles SET status='invitation_sent' WHERE id=1;")
    second = json.loads(sql('SELECT row_to_json(o) FROM logto_sync_outbox o;'))
    assert (second['generation'], second['role_status'], second['state']) == (3, 'invitation_sent', 'pending')
    assert sql("SELECT status FROM logto_identity_binding WHERE local_user_id='u1';") == 'disabled'
    sql("INSERT INTO user_roles(user_id,org_id,merchant_id,role_id,status) VALUES "
        "('u1','o1','m1','ordinary_user','active');")
    assert sql('SELECT count(*) FROM logto_sync_outbox;') == '1'
    os.environ['DOCKER_HOST'] = env['DOCKER_HOST']
    database = Database(['docker', 'exec', '-i', '-e', f'PGOPTIONS=-c search_path={schema}',
                         'hyperswitch-debug-pg', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                         '-U', 'db_user', '-d', 'hyperswitch_db'])
    claimed = database.claim()
    assert claimed['id'] == first['id'] and claimed['state'] == 'leased'
    database.finish_unprivileged(claimed)
    assert sql('SELECT state FROM logto_sync_outbox;') == 'done'
    sql("INSERT INTO user_roles(user_id,org_id,merchant_id,role_id,status) VALUES "
        "('u1','o1','m1','collection_merchant_view_only','active');")
    active = database.claim()
    database.finish(active, 'logto_u1', 'logto_o1')
    assert sql("SELECT status FROM logto_identity_binding WHERE role_id='collection_merchant_view_only';") == 'ready'
    assert sql(f'SELECT state FROM logto_sync_outbox WHERE id={active["id"]};') == 'done'
    sql("UPDATE users SET is_active=FALSE WHERE user_id='u1';")
    assert sql("SELECT status FROM logto_identity_binding WHERE role_id='collection_merchant_view_only';") == 'disabled'
    assert sql(f'SELECT state FROM logto_sync_outbox WHERE id={active["id"]};') == 'pending'
    sql("UPDATE users SET is_active=TRUE WHERE user_id='u1';")
    assert sql(f'SELECT state FROM logto_sync_outbox WHERE id={active["id"]};') == 'pending'
    sql("UPDATE user_roles SET merchant_id='m2' WHERE id=3;")
    assert sql("SELECT status FROM logto_identity_binding WHERE role_id='collection_merchant_view_only';") == 'disabled'
    print('Logto outbox migration: 13 checks passed')
finally:
    sql(f'DROP SCHEMA IF EXISTS {schema} CASCADE;')
