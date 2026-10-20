#!/usr/bin/env python3
"""RSA-only EPay V2 ingress for Hyperswitch V1. Python stdlib + OpenSSL.

Run one gateway instance per SQLite ledger. Native payment execution and channel
quota enforcement remain in Hyperswitch. No merchant-supplied routing is forwarded.
"""
import argparse
import base64
import contextlib
import hashlib
import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

MAX_BODY = 65536
FIELDS = set('pid type out_trade_no notify_url return_url name money param clientip device method timestamp sign sign_type trade_no out_refund_no refund_no'.split())

class ProtocolError(Exception):
    pass


def canonical(params):
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in params.items()):
        raise ProtocolError('Parameters must be strings')
    return '&'.join(f'{k}={v}' for k, v in sorted(params.items()) if v.strip(' \t\n\r\x00\x0b') != '' and k not in ('sign', 'sign_type')).encode('utf-8')


def rsa_sign(params, private_key):
    result = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(private_key)], input=canonical(params), capture_output=True, timeout=10)
    if result.returncode:
        raise RuntimeError('Platform signing failed')
    return base64.b64encode(result.stdout).decode('ascii')


def rsa_verify(params, public_key):
    try:
        signature = base64.b64decode(params.get('sign', ''), validate=True)
        if not 256 <= len(signature) <= 1024:
            return False
        with tempfile.NamedTemporaryFile() as f:
            f.write(signature)
            f.flush()
            result = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(public_key), '-signature', f.name], input=canonical(params), capture_output=True, timeout=10)
        return result.returncode == 0
    except (ValueError, subprocess.TimeoutExpired):
        return False


def money_minor(value):
    if not re.fullmatch(r'(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,2})?', value):
        raise ProtocolError('Invalid money')
    amount = int(Decimal(value) * 100)
    if amount <= 0:
        raise ProtocolError('Invalid money')
    return amount


def validate_url(value, allow_loopback=False):
    try:
        u = urllib.parse.urlsplit(value)
        if u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password or u.fragment:
            raise ValueError()
        addresses = {x[4][0] for x in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80), type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            if not (allow_loopback and addresses and all(ipaddress.ip_address(a).is_loopback for a in addresses)):
                raise ValueError()
        if u.scheme != 'https' and not (allow_loopback and all(ipaddress.ip_address(a).is_loopback for a in addresses)):
            raise ValueError()
        return u, sorted(addresses)
    except (ValueError, OSError):
        raise ProtocolError('Unsafe callback or return URL') from None


def send_callback(url, params, allow_loopback=False):
    # Resolve once, validate the actual target and connect to that IP. Never follow
    # redirects or re-resolve between validation and connection (DNS rebinding).
    import http.client
    u, addresses = validate_url(url, allow_loopback)
    address = addresses[0]
    port = u.port or (443 if u.scheme == 'https' else 80)
    host = u.hostname
    sock = socket.create_connection((address, port), timeout=10)
    if u.scheme == 'https':
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.sock = sock
    query = urllib.parse.urlencode(params)
    target = (u.path or '/') + '?' + (u.query + '&' if u.query else '') + query
    try:
        conn.request('GET', target, headers={'Host': u.netloc, 'Connection': 'close'})
        response = conn.getresponse()
        body = response.read(1025)
        return 200 <= response.status < 300 and len(body) <= 1024 and body.strip() == b'success'
    finally:
        conn.close()


class Gateway:
    def __init__(self, config):
        self.config = config
        self.db_path = config['database']
        self.private_key = config['platform_private_key']
        if Path(self.private_key).stat().st_mode & 0o077:
            raise RuntimeError('Platform private key must have mode 0600')
        key_info = subprocess.run(['openssl', 'pkey', '-in', str(self.private_key), '-passin', 'pass:', '-text_pub', '-noout'], capture_output=True, timeout=10)
        if key_info.returncode or b'Modulus:' not in key_info.stdout or not any(f'({bits} bit)'.encode() in key_info.stdout for bits in (2048, 3072, 4096)):
            raise RuntimeError('Platform key must be RSA 2048/3072/4096')
        self.merchants = config['merchants']
        validate_url(config['default_return_url'], config.get('test_allow_loopback_callbacks', False))
        self.public_url = config['public_url'].rstrip('/')
        validate_url(self.public_url, config.get('test_allow_loopback_callbacks', False))
        for pid, m in self.merchants.items():
            if not re.fullmatch(r'[0-9]{1,20}', pid) or m.get('currency', 'CNY') not in ('CNY', 'USD', 'EUR', 'HKD', 'GBP'):
                raise RuntimeError('Invalid merchant ID or two-decimal currency')
            if not m.get('public_keys') or not m.get('types') or not m.get('profile_id') or not m.get('api_key') or not m.get('merchant_id'):
                raise RuntimeError('Incomplete merchant configuration')
            for key in m['public_keys']:
                result = subprocess.run(['openssl', 'pkey', '-pubin', '-in', key, '-text', '-noout'], capture_output=True, timeout=10)
                if result.returncode or b'RSA' not in result.stdout + result.stderr and b'Modulus:' not in result.stdout or b'(2048 bit)' not in result.stdout and b'(3072 bit)' not in result.stdout and b'(4096 bit)' not in result.stdout:
                    raise RuntimeError('Merchant public key must be RSA 2048/3072/4096')
        base = urllib.parse.urlsplit(config['backend_url'])
        if base.scheme != 'https' and not (base.scheme == 'http' and base.hostname in ('127.0.0.1', 'localhost', '::1')):
            raise RuntimeError('Backend must use HTTPS or loopback HTTP')
        Path(self.db_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS orders (
              pid TEXT NOT NULL, out_trade_no TEXT NOT NULL, trade_no TEXT NOT NULL UNIQUE,
              fingerprint TEXT NOT NULL, params TEXT NOT NULL, pay_info TEXT, return_token TEXT,
              status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL, poll_due INTEGER NOT NULL DEFAULT 0,
              paid_at INTEGER, notification_attempts INTEGER NOT NULL DEFAULT 0,
              notification_due INTEGER NOT NULL DEFAULT 0, notification_done INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(pid,out_trade_no));
            ''')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS refunds (
              pid TEXT NOT NULL, out_refund_no TEXT NOT NULL, refund_no TEXT NOT NULL UNIQUE,
              trade_no TEXT NOT NULL REFERENCES orders(trade_no), amount INTEGER NOT NULL CHECK(amount>0),
              currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'created',
              lease_until INTEGER NOT NULL DEFAULT 0, poll_due INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              PRIMARY KEY(pid,out_refund_no));
            CREATE INDEX IF NOT EXISTS refunds_order ON refunds(pid,trade_no);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(orders)')}
            if 'return_token' not in columns:
                db.execute('ALTER TABLE orders ADD COLUMN return_token TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS orders_return_token ON orders(return_token)')
        os.chmod(self.db_path, 0o600)
        self.stop = threading.Event()

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def signed(self, payload):
        # Keep numeric JSON fields numeric; canonical signing uses their textual form.
        payload = dict(payload, timestamp=int(time.time()), sign_type='RSA')
        payload['sign'] = rsa_sign({k: str(v) for k, v in payload.items() if v is not None}, self.private_key)
        return payload

    def authenticate(self, params):
        if set(params) - FIELDS or params.get('sign_type', 'RSA') != 'RSA':
            raise ProtocolError('Only EPay V2 RSA is supported')
        stamp = params.get('timestamp', '')
        if not re.fullmatch(r'[0-9]{10}', stamp) or abs(int(time.time()) - int(stamp)) > 300:
            raise ProtocolError('Expired or invalid timestamp')
        merchant = self.merchants.get(params.get('pid'))
        if not merchant or not any(rsa_verify(params, key) for key in merchant['public_keys']):
            raise ProtocolError('Invalid merchant or signature')
        return merchant

    def backend_json(self, merchant, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.config['backend_url'].rstrip('/') + path, data=data, method=method, headers={
            'api-key': merchant['api_key'], 'Content-Type': 'application/json', 'X-Merchant-Id': merchant['merchant_id'], 'X-Profile-Id': merchant['profile_id']})
        # Backend URL is administrator configured. Refuse redirects to avoid leaking keys.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(req, timeout=20) as response:
                result = json.loads(response.read(1024 * 1024))
            if not isinstance(result, dict):
                raise ProtocolError('Invalid backend response')
            return result
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise ProtocolError('Payment backend rejected request') from None
        except (urllib.error.URLError, TimeoutError):
            raise ProtocolError('Payment backend unavailable; retry same order number') from None

    def backend(self, merchant, method, path, body=None):
        result = self.backend_json(merchant, method, path, body)
        if result is not None and (result.get('merchant_id') != merchant['merchant_id'] or result.get('profile_id') != merchant['profile_id']):
            raise ProtocolError('Backend merchant mismatch')
        return result

    def backend_refund(self, merchant, method, path, refund, body=None):
        # RefundResponse V1 has no merchant_id. The merchant-bound native API and
        # these immutable ledger associations establish ownership; never relax the
        # payment response merchant check just to accommodate a different schema.
        result = self.backend_json(merchant, method, path, body)
        if result is not None and (
            result.get('refund_id') != refund['refund_no']
            or result.get('payment_id') != refund['trade_no']
            or result.get('amount') != refund['amount']
            or result.get('currency') != refund['currency']
            or result.get('merchant_id', merchant['merchant_id']) != merchant['merchant_id']
            or result.get('profile_id') not in (None, merchant['profile_id'])
        ):
            raise ProtocolError('Backend refund does not match merchant order')
        return result

    def create(self, params, merchant):
        for field in ('out_trade_no', 'type', 'name', 'money', 'notify_url'):
            if not params.get(field):
                raise ProtocolError(f'Missing {field}')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', params['out_trade_no']) or len(params['name']) > 127 or len(params.get('param', '')) > 1024:
            raise ProtocolError('Invalid order fields')
        if params['type'] not in merchant['types'] or (params.get('method') or 'web') not in ('web', 'jump'):
            raise ProtocolError('Unsupported payment type or method')
        allowed_types = merchant.get('payment_method_types', {}).get(params['type'], {'alipay':['ali_pay'], 'wxpay':['we_chat_pay']}.get(params['type'], []))
        if not allowed_types:
            raise ProtocolError('Payment type has no configured backend methods')
        amount = money_minor(params['money'])
        allow_loopback = self.config.get('test_allow_loopback_callbacks', False)
        validate_url(params['notify_url'], allow_loopback)
        if params.get('return_url'):
            validate_url(params['return_url'], allow_loopback)
        for url in (params['notify_url'], params.get('return_url', '')):
            if url and set(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))) & (FIELDS | {'trade_status'}):
                raise ProtocolError('Callback URLs cannot contain protocol parameter names')
        identity = (params['pid'] + '\0' + params['out_trade_no']).encode()
        trade_no = 'pay_' + hashlib.sha256(identity).hexdigest()[:32]
        business = {k: params.get(k, '') for k in ('type', 'name', 'notify_url', 'return_url', 'param')}
        business.update(amount=amount, currency=merchant.get('currency', 'CNY'), profile_id=merchant['profile_id'])
        fingerprint = hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()
        # Persist before contacting backend. A crash can be recovered through the stable
        # native payment_id; never create a second payment for a retried merchant order.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM orders WHERE pid=? AND out_trade_no=?', (params['pid'], params['out_trade_no'])).fetchone()
            if row and row['fingerprint'] != fingerprint:
                raise ProtocolError('Order number already used with different fields')
            return_token = row['return_token'] if row and row['return_token'] else secrets.token_urlsafe(32)
            if not row:
                db.execute('INSERT INTO orders(pid,out_trade_no,trade_no,fingerprint,params,created_at,return_token) VALUES(?,?,?,?,?,?,?)',
                           (params['pid'], params['out_trade_no'], trade_no, fingerprint, json.dumps(params), int(time.time()), return_token))
            elif not row['return_token']:
                db.execute('UPDATE orders SET return_token=? WHERE trade_no=?', (return_token, trade_no))
            if row and row['pay_info']:
                return {'code': 0, 'trade_no': trade_no, 'pay_type': 'jump', 'pay_info': row['pay_info']}
        payment = self.backend(merchant, 'GET', '/payments/' + trade_no)
        if payment is None:
            body = {'payment_id': trade_no, 'amount': amount, 'currency': business['currency'], 'confirm': False,
                    'payment_link': True, 'profile_id': merchant['profile_id'], 'description': params['name'],
                    'merchant_order_reference_id': params['out_trade_no'],
                    'allowed_payment_method_types': merchant.get('payment_method_types', {}).get(params['type'], {'alipay':['ali_pay'], 'wxpay':['we_chat_pay']}.get(params['type'], []))}
            body['return_url'] = self.public_url + '/return/' + return_token
            try:
                payment = self.backend(merchant, 'POST', '/payments', body)
            except ProtocolError:
                payment = self.backend(merchant, 'GET', '/payments/' + trade_no)
                if payment is None:
                    raise
        if not payment or payment.get('amount') != amount or payment.get('currency') != business['currency']:
            raise ProtocolError('Backend payment does not match order')
        link = (payment.get('payment_link') or {}).get('link')
        if not link:
            raise ProtocolError('Hosted payment link unavailable')
        with self.db() as db:
            db.execute('UPDATE orders SET pay_info=? WHERE pid=? AND out_trade_no=?', (link, params['pid'], params['out_trade_no']))
        return {'code': 0, 'trade_no': trade_no, 'pay_type': 'jump', 'pay_info': link}

    def find(self, params):
        if bool(params.get('trade_no')) == bool(params.get('out_trade_no')):
            raise ProtocolError('Supply exactly one order identifier')
        field = 'trade_no' if params.get('trade_no') else 'out_trade_no'
        with self.db() as db:
            row = db.execute(f'SELECT * FROM orders WHERE pid=? AND {field}=?', (params['pid'], params[field])).fetchone()
        if not row:
            raise ProtocolError('Order not found')
        return row

    def refresh(self, row, merchant):
        payment = self.backend(merchant, 'GET', '/payments/' + row['trade_no'])
        if not payment:
            return None
        params = json.loads(row['params'])
        if payment.get('amount') != money_minor(params['money']) or payment.get('currency') != merchant.get('currency', 'CNY'):
            raise ProtocolError('Payment amount or currency mismatch')
        status = payment.get('status')
        if status == 'succeeded':
            with self.db() as db:
                db.execute("UPDATE orders SET status='paid',paid_at=COALESCE(paid_at,?),notification_due=CASE WHEN paid_at IS NULL THEN ? ELSE notification_due END WHERE trade_no=?", (int(time.time()), int(time.time()), row['trade_no']))
        return payment

    def query(self, params, merchant):
        row = self.find(params)
        payment = self.refresh(row, merchant)
        data = json.loads(row['params'])
        return {'code': 0, 'trade_no': row['trade_no'], 'out_trade_no': row['out_trade_no'],
                'type': data['type'], 'money': format(Decimal(money_minor(data['money'])) / 100, '.2f'),
                'name': data['name'], 'param': data.get('param', ''),
                'status': 1 if payment and payment.get('status') == 'succeeded' else 0}

    @staticmethod
    def refund_state(status):
        # Internal transaction failures/manual review can be ambiguous. Keep their
        # reservation until native reconciliation confirms success or failure.
        return {'succeeded':'success', 'failed':'failure', 'success':'success', 'failure':'failure',
                'pending':'pending', 'review':'pending', 'manual_review':'pending'}.get(str(status).lower(), 'unknown')

    def native_refund_state(self, result):
        state=self.refund_state(result.get('status'))
        # Native timeout handling deliberately clears the error code. Its public
        # failed status alone is insufficient evidence to free a financial hold.
        if state=='failure' and not result.get('error_code'):
            return 'unknown'
        return state

    def record_refund(self, row, result):
        state = self.native_refund_state(result)
        with self.db() as db:
            db.execute("UPDATE refunds SET status=CASE WHEN status='success' THEN status ELSE ? END, lease_until=0, poll_due=?, updated_at=? WHERE pid=? AND out_refund_no=?",
                       (state, int(time.time())+30, int(time.time()), row['pid'], row['out_refund_no']))
            return db.execute('SELECT * FROM refunds WHERE pid=? AND out_refund_no=?', (row['pid'],row['out_refund_no'])).fetchone()

    def refund_response(self, row, creation=False):
        with self.db() as db:
            order = db.execute('SELECT out_trade_no FROM orders WHERE pid=? AND trade_no=?', (row['pid'],row['trade_no'])).fetchone()
        success = row['status'] == 'success'
        failed = row['status'] == 'failure'
        uncertain = row['status'] in ('created','unknown')
        return {'code': -1 if creation and (failed or uncertain) else 0,
                'msg': 'Refund succeeded' if success else 'Refund failed' if failed else 'Refund outcome pending; retry the same refund number or query',
                'refund_no':row['refund_no'], 'out_refund_no':row['out_refund_no'],
                'trade_no':row['trade_no'], 'out_trade_no':order['out_trade_no'],
                'money':format(Decimal(row['amount'])/100,'.2f'),
                'status':1 if success else 0 if failed else 2,
                'refund_status':row['status'],
                'addtime':time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(row['created_at']))}

    def refund(self, params, merchant):
        order = self.find(params)
        payment = self.refresh(order,merchant)
        if payment is None:
            raise ProtocolError('Refund order is unavailable for this merchant')
        amount = money_minor(params.get('money',''))
        external_id = params.get('out_refund_no') or 'default_' + hashlib.sha256(order['trade_no'].encode()).hexdigest()[:32]
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', external_id):
            raise ProtocolError('Invalid out_refund_no')
        currency = merchant.get('currency','CNY')
        now = int(time.time())
        with self.db() as db:
            prior = db.execute('SELECT * FROM refunds WHERE pid=? AND out_refund_no=?', (params['pid'],external_id)).fetchone()
        if prior and (prior['trade_no'] != order['trade_no'] or prior['amount'] != amount or prior['currency'] != currency):
            raise ProtocolError('Refund number already used with different order or amount')
        if prior:
            native = self.backend_refund(merchant,'GET','/refunds/'+prior['refund_no'],prior)
            if native is not None:
                return self.refund_response(self.record_refund(prior,native),True)
            if prior['status'] in ('success','failure'):
                return self.refund_response(prior,True)
        if payment.get('status') not in ('succeeded','partially_captured'):
            raise ProtocolError('Only captured payments can be refunded')
        captured = payment.get('amount_received')
        if captured is None and payment['status']=='succeeded':
            captured = payment['amount']
        if not isinstance(captured,int) or isinstance(captured,bool) or captured<=0:
            raise ProtocolError('Captured amount unavailable')
        captured = min(captured,money_minor(json.loads(order['params'])['money']))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM refunds WHERE pid=? AND out_refund_no=?', (params['pid'],external_id)).fetchone()
            if row and (row['trade_no']!=order['trade_no'] or row['amount']!=amount or row['currency']!=currency):
                raise ProtocolError('Refund number already used with different order or amount')
            if not row:
                local = db.execute('SELECT refund_no,amount,status FROM refunds WHERE pid=? AND trade_no=?', (params['pid'],order['trade_no'])).fetchall()
                known = {r['refund_no'] for r in local}
                occupied = sum(r['amount'] for r in local if r['status']!='failure')
                # Include refunds issued through native APIs outside this gateway.
                for other in payment.get('refunds') or []:
                    if other.get('refund_id') not in known and self.native_refund_state(other)!='failure':
                        value=other.get('amount')
                        if not isinstance(value,int) or isinstance(value,bool) or value<=0:
                            raise ProtocolError('Native refund amount unavailable')
                        occupied+=value
                if amount+occupied>captured:
                    raise ProtocolError('Refund exceeds remaining captured amount')
                refund_no='ref_'+hashlib.sha256((params['pid']+'\0'+external_id).encode()).hexdigest()[:26]
                db.execute('INSERT INTO refunds(pid,out_refund_no,refund_no,trade_no,amount,currency,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                           (params['pid'],external_id,refund_no,order['trade_no'],amount,currency,now,now))
                row=db.execute('SELECT * FROM refunds WHERE pid=? AND out_refund_no=?', (params['pid'],external_id)).fetchone()
            if row['lease_until']>now or row['status'] in ('success','failure'):
                return self.refund_response(row,True)
            db.execute('UPDATE refunds SET lease_until=? WHERE pid=? AND out_refund_no=?', (now+60,params['pid'],external_id))
        # GET first recovers a crash after native creation but before local storage.
        # Retried POSTs use the SAME native refund_id. Native unique insertion occurs
        # before connector execution, preventing a duplicate financial side effect.
        try:
            native = self.backend_refund(merchant,'GET','/refunds/'+row['refund_no'],row)
            if native is None:
                native=self.backend_refund(merchant,'POST','/refunds',row,{
                    'payment_id':row['trade_no'],'refund_id':row['refund_no'],
                    'amount':amount,'refund_type':'instant','reason':'requested_by_customer'})
            if native is None:
                raise ProtocolError('Refund backend unavailable')
            row=self.record_refund(row,native)
        except ProtocolError:
            try:
                native=self.backend_refund(merchant,'GET','/refunds/'+row['refund_no'],row)
            except ProtocolError:
                native=None
            if native is not None:
                row=self.record_refund(row,native)
            else:
                with self.db() as db:
                    db.execute("UPDATE refunds SET status=CASE WHEN status IN ('success','failure') THEN status ELSE 'unknown' END,lease_until=0,updated_at=? WHERE pid=? AND out_refund_no=?",(now,row['pid'],row['out_refund_no']))
                    row=db.execute('SELECT * FROM refunds WHERE pid=? AND out_refund_no=?',(row['pid'],row['out_refund_no'])).fetchone()
        return self.refund_response(row,True)

    def refund_query(self, params, merchant):
        if bool(params.get('refund_no')) == bool(params.get('out_refund_no')):
            raise ProtocolError('Supply exactly one refund identifier')
        field='refund_no' if params.get('refund_no') else 'out_refund_no'
        with self.db() as db:
            row=db.execute(f'SELECT * FROM refunds WHERE pid=? AND {field}=?',(params['pid'],params[field])).fetchone()
        if not row:
            raise ProtocolError('Refund not found')
        with self.db() as db:
            order=db.execute('SELECT * FROM orders WHERE pid=? AND trade_no=?',(row['pid'],row['trade_no'])).fetchone()
        if not order or self.refresh(order,merchant) is None:
            raise ProtocolError('Refund order is unavailable for this merchant')
        # Explicit querying may sync an existing native refund; it never creates one.
        try:
            native=self.backend_refund(merchant,'GET','/refunds/'+row['refund_no']+'?force_sync=true',row)
        except ProtocolError:
            native=None
        if native is not None:
            row=self.record_refund(row,native)
        return self.refund_response(row)

    def handle(self, path, params):
        merchant = self.authenticate(params)
        if path in ('/api/pay/refund','/api/pay/refundquery'):
            allowed = set('pid timestamp sign sign_type'.split()) | (set('trade_no out_trade_no out_refund_no money'.split()) if path.endswith('/refund') else {'refund_no','out_refund_no'})
            if set(params)-allowed:
                raise ProtocolError('Unsupported refund parameters')
            return self.signed(self.refund(params,merchant) if path.endswith('/refund') else self.refund_query(params,merchant))
        if path == '/api/pay/submit' and not params.get('type'):
            selected = merchant.get('default_type') or (merchant['types'][0] if len(merchant['types']) == 1 else '')
            params = dict(params, type=selected)
        if path in ('/api/pay/create', '/api/pay/submit'):
            return self.signed(self.create(params, merchant))
        if path == '/api/pay/query':
            return self.signed(self.query(params, merchant))
        raise ProtocolError('Unsupported V2 endpoint')

    def return_redirect(self, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise ProtocolError('Invalid return token')
        with self.db() as db:
            row = db.execute('SELECT * FROM orders WHERE return_token=?', (token,)).fetchone()
        if not row:
            raise ProtocolError('Order not found')
        merchant = self.merchants.get(row['pid'])
        if not merchant:
            raise ProtocolError('Merchant unavailable')
        payment = self.refresh(row, merchant)
        if not payment or payment.get('status') != 'succeeded':
            return None
        p = json.loads(row['params'])
        target = p.get('return_url') or self.config['default_return_url']
        validate_url(target, self.config.get('test_allow_loopback_callbacks', False))
        payload = {k: p[k] for k in ('pid', 'type', 'out_trade_no', 'name', 'money')}
        payload.update(trade_no=row['trade_no'], trade_status='TRADE_SUCCESS', param=p.get('param', ''))
        parsed = urllib.parse.urlsplit(target)
        query = (parsed.query + '&' if parsed.query else '') + urllib.parse.urlencode(self.signed(payload))
        return urllib.parse.urlunsplit(parsed._replace(query=query))

    def tick(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM orders WHERE (status='pending' AND poll_due<=?) OR (status='paid' AND notification_done=0 AND notification_attempts<10 AND notification_due<=?) ORDER BY CASE status WHEN 'paid' THEN 0 ELSE 1 END,poll_due,notification_due LIMIT 100", (int(time.time()),int(time.time()))).fetchall()
        for row in rows:
            try:
                merchant = self.merchants.get(row['pid'])
                if not merchant:
                    continue
                if row['status'] != 'paid':
                    with self.db() as db:
                        db.execute('UPDATE orders SET poll_due=? WHERE trade_no=?', (int(time.time())+15, row['trade_no']))
                    self.refresh(row, merchant)
                    continue
                p = json.loads(row['params'])
                notification = {k: p[k] for k in ('pid', 'type', 'out_trade_no', 'name', 'money')}
                notification.update(trade_no=row['trade_no'], trade_status='TRADE_SUCCESS', param=p.get('param', ''))
                notification = self.signed(notification)
                # Persist the attempt before the network call. Delivery is at-least-once.
                with self.db() as db:
                    db.execute('UPDATE orders SET notification_attempts=notification_attempts+1,notification_due=? WHERE trade_no=?', (int(time.time()) + min(3600, 30 * 2 ** row['notification_attempts']), row['trade_no']))
                if send_callback(p['notify_url'], notification, self.config.get('test_allow_loopback_callbacks', False)):
                    with self.db() as db:
                        db.execute('UPDATE orders SET notification_done=1 WHERE trade_no=?', (row['trade_no'],))
            except Exception:
                # Never log URLs, personal data, credentials, request bodies or signatures.
                continue

        with self.db() as db:
            refunds=db.execute("SELECT * FROM refunds WHERE status NOT IN ('success','failure') AND lease_until<=? AND poll_due<=? ORDER BY poll_due LIMIT 100",(int(time.time()),int(time.time()))).fetchall()
        for row in refunds:
            try:
                merchant=self.merchants.get(row['pid'])
                if not merchant:
                    continue
                with self.db() as db:
                    db.execute('UPDATE refunds SET poll_due=? WHERE pid=? AND out_refund_no=?',(int(time.time())+30,row['pid'],row['out_refund_no']))
                native=self.backend_refund(merchant,'GET','/refunds/'+row['refund_no'],row)
                if native is not None:
                    self.record_refund(row,native)
            except Exception:
                continue

    def worker(self):
        while not self.stop.wait(5):
            try:
                self.tick()
            except Exception:
                continue


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'EPayV2'
    def log_message(self, *args):
        pass
    def do_GET(self):
        self.dispatch()
    def do_POST(self):
        self.dispatch()
    def dispatch(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if self.command == 'GET' and parsed.path.startswith('/return/'):
                # Native redirect query fields (including status) are untrusted and ignored.
                destination = self.server.gateway.return_redirect(parsed.path.removeprefix('/return/'))
                self.send_response(302 if destination else 200)
                if destination:
                    self.send_header('Location', destination)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                if not destination:
                    self.wfile.write(b'Payment has not been confirmed. Please query the order status.')
                return
            if parsed.path not in ('/api/pay/create', '/api/pay/submit', '/api/pay/query', '/api/pay/refund', '/api/pay/refundquery'):
                self.send_error(404)
                return
            if self.command == 'GET' and parsed.path != '/api/pay/submit':
                raise ProtocolError('POST required')
            text = parsed.query
            if self.command == 'POST':
                if self.headers.get_content_type() != 'application/x-www-form-urlencoded' or parsed.query:
                    raise ProtocolError('Use form-urlencoded body without query parameters')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                    raise ProtocolError('Invalid body length')
                text = self.rfile.read(length).decode('utf-8', errors='strict')
            if len(text) > MAX_BODY:
                raise ProtocolError('Request too large')
            pairs = urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=True, max_num_fields=32, errors='strict')
            params = dict(pairs)
            if len(params) != len(pairs):
                raise ProtocolError('Duplicate parameters')
            result = self.server.gateway.handle(parsed.path, params)
            if parsed.path == '/api/pay/submit':
                self.send_response(302)
                self.send_header('Location', result['pay_info'])
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
        except (ProtocolError, ValueError, UnicodeError) as error:
            result = self.server.gateway.signed({'code': -1, 'msg': str(error) if isinstance(error, ProtocolError) else 'Invalid request'})
        except Exception:
            result = self.server.gateway.signed({'code': -1, 'msg': 'Gateway unavailable'})
        body = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Bound the number of expensive RSA verifications and backend requests.
    def __init__(self, address, gateway):
        self.gateway = gateway
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, address)
    def process_request_thread(self, request, address):
        try:
            request.settimeout(25)
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config_path = Path(args.config)
    if config_path.stat().st_mode & 0o077:
        raise SystemExit('Configuration must have mode 0600')
    os.umask(0o077)
    gateway = Gateway(json.loads(config_path.read_text()))
    threading.Thread(target=gateway.worker, daemon=True).start()
    server = Server((gateway.config.get('listen', '127.0.0.1'), gateway.config.get('port', 18100)), gateway)
    try:
        server.serve_forever()
    finally:
        gateway.stop.set()
        server.server_close()

if __name__ == '__main__':
    main()
