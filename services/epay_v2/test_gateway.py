import json
import http.server
from pathlib import Path
import subprocess
import tempfile
import time
import threading
import urllib.parse
import urllib.request
import urllib.error
import unittest
from unittest.mock import patch
from gateway import Gateway, ProtocolError, canonical, money_minor, rsa_sign, rsa_verify, validate_url, Server, send_callback

class GatewayFixture:
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        for name in ('platform', 'a', 'b'):
            private = cls.root / (name + '.pem')
            public = cls.root / (name + '.pub')
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(private)], capture_output=True, check=True)
            private.chmod(0o600)
            subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)], capture_output=True, check=True)
    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=self.root)
        config = {'database': self.folder.name + '/orders.db', 'platform_private_key': str(self.root/'platform.pem'), 'backend_url': 'http://127.0.0.1:18080', 'public_url':'http://127.0.0.1:18100',
                  'default_return_url': 'http://127.0.0.1:18100/complete', 'test_allow_loopback_callbacks': True,
                  'merchants': {pid: {'merchant_id': name, 'profile_id': 'profile_'+name, 'api_key': 'test_'+name,
                                    'public_keys': [str(self.root/(name+'.pub'))], 'currency':'USD', 'types':['alipay']}
                                for pid,name in [('1','a'),('2','b')]}}
        self.g = Gateway(config)
        self.payments = {}
        self.creates = 0
        def backend(merchant, method, path, body=None):
            key = (merchant['merchant_id'], path.split('/')[-1])
            if method == 'GET':
                return self.payments.get(key)
            self.creates += 1
            result = dict(body, merchant_id=merchant['merchant_id'], status='requires_payment_method', payment_link={'link':'https://checkout.example/pay'})
            self.payments[(merchant['merchant_id'],body['payment_id'])] = result
            return result
        self.g.backend = backend
    def tearDown(self):
        self.folder.cleanup()
    def params(self, **overrides):
        p = dict(pid='1',type='alipay',out_trade_no='ORDER1',money='10.00',name='商品 & test',notify_url='http://127.0.0.1:18101/notify',timestamp=str(int(time.time())),sign_type='RSA')
        p.update(overrides)
        p['sign'] = rsa_sign(p, self.root/('a.pem' if p['pid']=='1' else 'b.pem'))
        return p
class GatewayTests(GatewayFixture, unittest.TestCase):
    def test_signature_and_tampering(self):
        p=self.params()
        self.assertTrue(rsa_verify(p,self.root/'a.pub'))
        p['money']='20.00'
        self.assertFalse(rsa_verify(p,self.root/'a.pub'))
    def test_platform_response_signature(self):
        r=self.g.handle('/api/pay/create',self.params())
        self.assertTrue(rsa_verify({k:str(v) for k,v in r.items()},self.root/'platform.pub'))
    def test_reject_md5_and_expired_and_wrong_key(self):
        for p in [self.params(sign_type='MD5'),self.params(timestamp='1000000000'),dict(self.params(),pid='2')]:
            with self.assertRaises(ProtocolError): self.g.handle('/api/pay/create',p)
    def test_money_exact(self):
        self.assertEqual(money_minor('3000.01'),300001)
        for amount in ['1e3','NaN','-1','0','1.001','01.00','1,000']:
            with self.assertRaises(ProtocolError): money_minor(amount)
    def test_canonical(self):
        self.assertEqual(canonical({'b':'0','a':'中文','empty':'','sign':'ignored','sign_type':'RSA'}),'a=中文&b=0'.encode())
    def test_idempotency_and_conflict(self):
        one=self.g.handle('/api/pay/create',self.params())
        two=self.g.handle('/api/pay/create',self.params())
        self.assertEqual(one['trade_no'],two['trade_no'])
        self.assertEqual(self.creates,1)
        with self.assertRaises(ProtocolError): self.g.handle('/api/pay/create',self.params(money='20'))
    def test_cross_merchant_cannot_query(self):
        r=self.g.handle('/api/pay/create',self.params())
        with self.assertRaises(ProtocolError):
            self.g.handle('/api/pay/query',self.params(pid='2',trade_no=r['trade_no'],out_trade_no=''))
        other=self.g.handle('/api/pay/create',self.params(pid='2'))
        self.assertNotEqual(r['trade_no'],other['trade_no'])
    def test_only_authoritative_success_sends_notification(self):
        r=self.g.handle('/api/pay/create',self.params())
        with patch('gateway.send_callback',return_value=True) as deliver:
            self.g.tick()
            self.assertFalse(deliver.called)
            self.payments[('a',r['trade_no'])]['status']='succeeded'
            with self.g.db() as db: db.execute('UPDATE orders SET poll_due=0')
            self.g.tick()
            self.g.tick()
            self.assertEqual(deliver.call_count,1)
            payload=deliver.call_args.args[1]
            self.assertTrue(rsa_verify({k:str(v) for k,v in payload.items()},self.root/'platform.pub'))
            self.g.tick()
            self.assertEqual(deliver.call_count,1)
    def test_notifications_retry_after_non_ack(self):
        r=self.g.handle('/api/pay/create',self.params())
        self.payments[('a',r['trade_no'])]['status']='succeeded'
        self.g.tick()
        with patch('gateway.send_callback',return_value=False) as deliver:
            self.g.tick()
            self.g.tick()
            self.assertEqual(deliver.call_count,1)
        with self.g.db() as db:
            row=db.execute('SELECT * FROM orders').fetchone()
        self.assertEqual(row['notification_attempts'],1)
        self.assertEqual(row['notification_done'],0)
    def test_synchronous_return_only_signs_authoritative_success(self):
        result=self.g.handle('/api/pay/create',self.params(return_url='http://127.0.0.1:18101/return'))
        with self.g.db() as db: token=db.execute('SELECT return_token FROM orders').fetchone()[0]
        self.assertIsNone(self.g.return_redirect(token))
        self.payments[('a',result['trade_no'])]['status']='succeeded'
        target=self.g.return_redirect(token)
        fields=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(target).query))
        self.assertEqual(fields['trade_status'],'TRADE_SUCCESS')
        self.assertTrue(rsa_verify(fields,self.root/'platform.pub'))
        with self.assertRaises(ProtocolError): self.g.return_redirect('A'*43)

    def test_reject_unsafe_callbacks(self):
        for u in ['http://127.0.0.1/x','https://169.254.169.254/','https://user:pass@example.com/','file:///etc/passwd']:
            with self.assertRaises(ProtocolError): validate_url(u)
    def test_reject_unsupported_methods_or_channel_override(self):
        for p in [self.params(method='jsapi'),self.params(type='unknown'),self.params(connector='stripe')]:
            with self.assertRaises(ProtocolError): self.g.handle('/api/pay/create',p)
    def test_query_not_paid_until_backend_succeeded(self):
        self.g.handle('/api/pay/create',self.params())
        query=self.params()
        self.assertEqual(self.g.handle('/api/pay/query',query)['status'],0)
        next(iter(self.payments.values()))['status']='succeeded'
        self.assertEqual(self.g.handle('/api/pay/query',query)['status'],1)

class HttpTests(GatewayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server=Server(('127.0.0.1',0),self.g)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()
    def post(self, params, raw=None):
        data=(raw or urllib.parse.urlencode(params)).encode()
        req=urllib.request.Request(self.base+'/api/pay/create',data=data,headers={'Content-Type':'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req) as response: return json.load(response)
    def test_http_duplicate_and_md5_rejected(self):
        p=self.params()
        self.assertNotEqual(self.post(p,urllib.parse.urlencode(p)+'&pid=1')['code'],0)
        self.assertNotEqual(self.post(self.params(sign_type='MD5'))['code'],0)
    def test_real_callback_ack_and_redirect_refusal(self):
        class Callback(http.server.BaseHTTPRequestHandler):
            calls=0
            code=200
            body=b' success\n'
            def log_message(self,*args): pass
            def do_GET(self):
                Callback.calls+=1
                self.send_response(Callback.code)
                if Callback.code==302: self.send_header('Location','http://169.254.169.254/')
                self.end_headers()
                self.wfile.write(Callback.body)
        callback=http.server.HTTPServer(('127.0.0.1',0),Callback)
        thread=threading.Thread(target=callback.serve_forever,daemon=True);thread.start()
        target='http://127.0.0.1:'+str(callback.server_port)+'/notify'
        try:
            self.assertTrue(send_callback(target,self.params(),True))
            Callback.body=b'success extra'
            self.assertFalse(send_callback(target,self.params(),True))
            Callback.code=302
            self.assertFalse(send_callback(target,self.params(),True))
            self.assertEqual(Callback.calls,3)
        finally:
            callback.shutdown();callback.server_close();thread.join()

    def test_http_return_ignores_forged_browser_success(self):
        result=self.post(self.params())
        with self.g.db() as db: token=db.execute('SELECT return_token FROM orders').fetchone()[0]
        with urllib.request.urlopen(self.base+'/return/'+token+'?status=succeeded') as response:
            self.assertEqual(response.status,200)
            self.assertIn(b'not been confirmed',response.read())
        self.payments[('a',result['trade_no'])]['status']='succeeded'
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs): return None
        with self.assertRaises(urllib.error.HTTPError) as redirect:
            urllib.request.build_opener(NoRedirect).open(self.base+'/return/'+token)
        self.assertEqual(redirect.exception.code,302)
        fields=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(redirect.exception.headers['Location']).query))
        self.assertEqual(fields['trade_status'],'TRADE_SUCCESS')
        self.assertTrue(rsa_verify(fields,self.root/'platform.pub'))

    def test_http_signed_client_and_legacy_404(self):
        from client import request
        result=request({'api_url':self.base,'pid':'1','merchant_private_key':str(self.root/'a.pem'),'platform_public_key':str(self.root/'platform.pub')},'create',self.params())
        self.assertEqual(result['code'],0)
        with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(self.base+'/submit.php')
        self.assertEqual(error.exception.code,404)

if __name__=='__main__': unittest.main()
