#!/usr/bin/env python3
"""Provision Hyperswitch accounts into Logto from the global-user outbox.

Run a single worker against the global user database. Existing Logto users with
another ID are never claimed by matching email. No passwords are copied.
"""
import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


ROLES = {'org_admin', 'tenant_admin', 'collection_merchant_admin', 'collection_merchant_view_only'}


def logto_username(user_id):
    """Stable private username for Logto policies that require one."""
    return 'hs_' + hashlib.sha256(user_id.encode('utf-8')).hexdigest()[:40]


def sql_value(value):
    if value is None:
        return 'NULL'
    encoded = str(value).encode('utf-8').hex()
    return "convert_from(decode('" + encoded + "','hex'),'UTF8')"


class Database:
    def __init__(self, command):
        if not command or not isinstance(command, list) or not all(isinstance(x, str) for x in command):
            raise ValueError('db_command must be a nonempty array of program arguments')
        self.command = command

    def run(self, query):
        result = subprocess.run(self.command, input=query + '\n', text=True,
                                capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError('Global user database command failed: ' + result.stderr[-500:])
        return result.stdout.strip()

    def json(self, query):
        result = self.run(query.rstrip(';') + ';')
        return json.loads(result or 'null')

    def claim(self):
        return self.json('''WITH picked AS (
          SELECT id FROM logto_sync_outbox
          WHERE (state='pending' OR (state='leased' AND lease_until<now()))
            AND next_attempt_at<=now()
          ORDER BY next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT 1
        ), updated AS (
          UPDATE logto_sync_outbox o SET state='leased', lease_until=now()+interval '5 minutes',
            attempt_count=attempt_count+1, updated_at=now()
          FROM picked WHERE o.id=picked.id RETURNING o.*
        ) SELECT row_to_json(updated) FROM updated''')

    def user(self, user_id):
        return self.json('SELECT row_to_json(u) FROM (SELECT user_id,email,name,is_verified,is_active '
                         'FROM users WHERE user_id=' + sql_value(user_id) + ') u')

    def org_id(self, local_org_id):
        return self.json('SELECT to_json(logto_organization_id) FROM logto_organization_binding '
                         'WHERE local_org_id=' + sql_value(local_org_id))

    def save_org(self, local_org_id, logto_org_id):
        self.run('INSERT INTO logto_organization_binding(local_org_id,logto_organization_id) VALUES ('
                 + sql_value(local_org_id) + ',' + sql_value(logto_org_id) + ') '
                 'ON CONFLICT(local_org_id) DO NOTHING;')
        if self.org_id(local_org_id) != logto_org_id:
            raise RuntimeError('Conflicting Logto organization mapping')

    def finish(self, item, subject, org_id):
        role = item['role_id']
        if role not in ROLES:
            raise ValueError('Unsupported role')
        merchant = item['local_merchant_id'] if role != 'tenant_admin' else None
        local_org = item['local_org_id'] if role != 'tenant_admin' else None
        logto_org = org_id if role != 'tenant_admin' else None
        identity = [item['local_user_id'], local_org, merchant, subject, logto_org, role]
        values = ','.join(sql_value(value) for value in identity)
        item_id, generation = int(item['id']), int(item['generation'])
        # Generation fencing prevents a stale worker from installing an old role.
        query = f'''WITH current AS (
            SELECT id FROM logto_sync_outbox WHERE id={item_id} AND generation={generation}
            AND state='leased' FOR UPDATE
          ), bound AS (INSERT INTO logto_identity_binding
            (local_user_id,local_org_id,local_merchant_id,logto_subject,logto_organization_id,role_id,status)
            SELECT {values},'ready' FROM current
            ON CONFLICT(local_user_id,role_id) DO UPDATE SET
              status='ready',updated_at=now()
              WHERE logto_identity_binding.logto_subject=EXCLUDED.logto_subject
                AND logto_identity_binding.local_org_id IS NOT DISTINCT FROM EXCLUDED.local_org_id
                AND logto_identity_binding.local_merchant_id IS NOT DISTINCT FROM EXCLUDED.local_merchant_id
                AND logto_identity_binding.logto_organization_id IS NOT DISTINCT FROM EXCLUDED.logto_organization_id
            RETURNING local_user_id
          ), completed AS (
            UPDATE logto_sync_outbox SET state='done',lease_until=NULL,last_error=NULL,updated_at=now()
            WHERE id={item_id} AND generation={generation} AND EXISTS (SELECT 1 FROM bound)
            RETURNING id
          ) SELECT count(*) FROM completed;'''
        if self.run(query) != '1':
            raise RuntimeError('Stale outbox generation or conflicting identity binding')

    def finish_unprivileged(self, item):
        result = self.run('UPDATE logto_sync_outbox SET state=\'done\',lease_until=NULL,'
                          'last_error=NULL,updated_at=now() '
                          f"WHERE id={int(item['id'])} AND generation={int(item['generation'])} "
                          "AND state='leased' RETURNING id;")
        if result != str(int(item['id'])):
            raise RuntimeError('Stale outbox generation')

    def retry(self, item, reason):
        delay = min(3600, 15 * 2 ** min(int(item['attempt_count']), 8))
        # No remote response body or credentials are stored in the outbox.
        self.run('UPDATE logto_sync_outbox SET state=\'pending\', lease_until=NULL, '
                 f'next_attempt_at=now()+interval \'{delay} seconds\', '
                 'last_error=' + sql_value(reason[:250]) + ', updated_at=now() '
                 f"WHERE id={int(item['id'])} AND generation={int(item['generation'])} AND state='leased';")

    def defer_manual(self, item):
        self.run('UPDATE logto_sync_outbox SET state=\'failed\',lease_until=NULL,'
                 "last_error='Platform administrator requires manual Logto role approval',"
                 'updated_at=now() '
                 f"WHERE id={int(item['id'])} AND generation={int(item['generation'])} "
                 "AND state='leased';")


class Logto:
    def __init__(self, config):
        self.api = config['management_api_base'].rstrip('/')
        self.token_url = config['token_url']
        self.resource = config['management_api_resource']
        self.client_id = config['m2m_client_id']
        self.client_secret = config['m2m_client_secret']
        for address in (self.api, self.token_url):
            parsed = urllib.parse.urlparse(address)
            if parsed.scheme == 'https':
                continue
            try:
                private = ipaddress.ip_address(parsed.hostname or '').is_private
            except ValueError:
                private = False
            if parsed.scheme != 'http' or not (config.get('allow_http_private_dev') and private):
                raise ValueError('Logto Management API and token URL require HTTPS')
        self.token = None
        self.expiry = 0

    def access_token(self):
        if self.token and time.monotonic() < self.expiry:
            return self.token
        credentials = base64.b64encode((self.client_id + ':' + self.client_secret).encode()).decode()
        payload = urllib.parse.urlencode({'grant_type':'client_credentials', 'resource':self.resource,
                                          'scope':'all'}).encode()
        request = urllib.request.Request(self.token_url, payload, headers={
            'Authorization':'Basic ' + credentials,
            'Content-Type':'application/x-www-form-urlencoded'}, method='POST')
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
        self.token = result['access_token']
        self.expiry = time.monotonic() + min(int(result.get('expires_in', 300)), 3600) - 30
        return self.token

    def call(self, method, path, body=None, allow_missing=False):
        target = self.api + path
        request = urllib.request.Request(target,
            None if body is None else json.dumps(body).encode(), method=method,
            headers={'Authorization':'Bearer ' + self.access_token(),
                     'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            if allow_missing and error.code == 404:
                return None
            raise RuntimeError(f'Logto {method} {path} returned HTTP {error.code}') from None

    def find_org_by_local_id(self, local_id):
        found = []
        for page in range(1, 101):
            rows = self.call('GET', f'/api/organizations?page={page}&page_size=100')
            for row in rows:
                if row.get('customData', {}).get('hyperswitch_org_id') == local_id:
                    found.append(row['id'])
            if len(rows) < 100:
                break
        else:
            raise RuntimeError('Logto organization scan exceeded 100 pages')
        if len(found) > 1:
            raise RuntimeError('Duplicate Logto organizations for local organization')
        return found[0] if found else None

    def ensure_org(self, db, local_id):
        existing = db.org_id(local_id)
        if existing:
            org = self.call('GET', '/api/organizations/' + urllib.parse.quote(existing), allow_missing=True)
            if not org or org.get('customData', {}).get('hyperswitch_org_id') != local_id:
                raise RuntimeError('Logto organization mapping is missing or altered')
            return existing
        found = self.find_org_by_local_id(local_id)
        if not found:
            created = self.call('POST', '/api/organizations', {
                'name': local_id[:128], 'customData': {'hyperswitch_org_id': local_id}})
            found = created['id']
        db.save_org(local_id, found)
        return found

    def ensure_user(self, user):
        user_id = user['user_id']
        username = logto_username(user_id)
        path = '/api/users/' + urllib.parse.quote(user_id, safe='')
        existing = self.call('GET', path, allow_missing=True)
        if not existing:
            self.call('POST', '/api/users', {'id':user_id, 'primaryEmail':user['email'],
                     'username':username, 'name':user['name'],
                     'customData':{'hyperswitch_user_id':user_id}})
            existing = self.call('GET', path)
        if existing.get('primaryEmail', '').lower() != user['email'].lower() or \
           existing.get('customData', {}).get('hyperswitch_user_id') != user_id:
            raise RuntimeError('Logto user ID or email belongs to another identity')
        if not existing.get('username'):
            self.call('PATCH', path, {'username': username})
            existing = self.call('GET', path)
        if existing.get('username') != username:
            raise RuntimeError('Logto user has a conflicting managed username')
        return user_id

    def provision(self, db, item):
        if item['role_id'] not in ROLES:
            raise RuntimeError('Role is not eligible for automatic provisioning')
        if item['role_status'].lower() != 'active':
            # The trigger disabled any existing local binding in the same role
            # transaction. Do not recreate identity or grant a remote role.
            db.finish_unprivileged(item)
            return
        user = db.user(item['local_user_id'])
        if not user or not user['is_verified'] or user['is_active'] is False:
            raise RuntimeError('Local user not active and verified yet')
        org_id = None
        if item['role_id'] != 'tenant_admin':
            if not item['local_org_id'] or not item['local_merchant_id']:
                raise RuntimeError('Merchant role lacks local organization or merchant')
            org_id = self.ensure_org(db, item['local_org_id'])
        subject = self.ensure_user(user)
        if org_id:
            path = '/api/organizations/' + urllib.parse.quote(org_id, safe='')
            self.call('POST', path + '/users', {'userIds':[subject]})
            if item['role_id'] != 'org_admin':
                self.call('POST', path + '/users/' + urllib.parse.quote(subject, safe='') + '/roles',
                          {'organizationRoleNames':[item['role_id']]})
        # Platform role assignment is intentionally handled as a separate explicit
        # platform approval; a new user must never self-elevate to tenant_admin.
        elif item['role_id'] == 'tenant_admin':
            db.defer_manual(item)
            return
        if item['role_id'] == 'org_admin':
            # Hyperswitch self-signup is only identity provisioning. A platform
            # collection-role grant is required before OIDC login is authorized.
            db.finish_unprivileged(item)
        else:
            db.finish(item, subject, org_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path, help='0600 JSON containing db_command and Logto M2M settings')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.config.stat().st_mode & 0o077:
        parser.error('Private config must be mode 0600')
    config = json.loads(args.config.read_text())
    db = Database(config['db_command'])
    logto = Logto(config)
    with args.config.with_suffix(args.config.suffix + '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            item = db.claim()
            if item:
                try:
                    logto.provision(db, item)
                    print(json.dumps({'id':item['id'], 'state':'done'}), flush=True)
                except Exception as error:
                    db.retry(item, str(error))
                    print(json.dumps({'id':item['id'], 'state':'retry', 'error':str(error)}),
                          file=sys.stderr, flush=True)
            if args.once:
                break
            time.sleep(5 if item else 15)


if __name__ == '__main__':
    main()
