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
    def test_pid_reassignment_cannot_reuse_cached_payment_link(self):
        self.g.handle('/api/pay/create',self.params())
        self.g.merchants['1'].update(merchant_id='b',profile_id='profile_b')
        with self.assertRaises(ProtocolError):
            self.g.handle('/api/pay/create',self.params())
    def test_pid_reassignment_stops_paid_notifications(self):
        result=self.g.handle('/api/pay/create',self.params())
        self.payments[('a',result['trade_no'])]['status']='succeeded'
        self.g.tick()
        self.g.merchants['1'].update(merchant_id='b',profile_id='profile_b')
        with patch('gateway.send_callback') as deliver:
            self.g.tick()
            self.assertFalse(deliver.called)
    def test_pid_reassignment_stops_refund_polling(self):
        self.g.handle('/api/pay/create',self.params())
        with self.g.db() as db:
            order=db.execute('SELECT * FROM orders').fetchone()
            db.execute('INSERT INTO refunds(pid,out_refund_no,refund_no,trade_no,amount,currency,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       ('1','R1','ref_test_reassignment',order['trade_no'],100,'USD',int(time.time()),int(time.time())))
            db.execute('UPDATE orders SET poll_due=?',(int(time.time())+3600,))
        self.g.merchants['1'].update(merchant_id='b',profile_id='profile_b')
        with patch.object(self.g,'backend_refund') as native:
            self.g.tick()
            self.assertFalse(native.called)
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

class RefundFixture(GatewayFixture):
    def setUp(self):
        super().setUp()
        self.refunds={}
        self.posted=[]
        self.refund_creates=0
        self.native_status='succeeded'
        self.native_lock=threading.Lock()
        def backend_refund(merchant,method,path,refund,body=None):
            with self.native_lock:
                key=(merchant['merchant_id'],refund['refund_no'])
                if method=='GET': return self.refunds.get(key)
                self.posted.append(dict(body))
                if key in self.refunds: raise ProtocolError('Duplicate native refund')
                self.refund_creates+=1
                result={'refund_id':body['refund_id'],'payment_id':body['payment_id'],'amount':body['amount'],'currency':refund['currency'],'profile_id':merchant['profile_id'],'status':self.native_status,'error_code':'mock_decline' if self.native_status=='failed' else None}
                self.refunds[key]=result
                return result
        self.normal_backend_refund=backend_refund
        self.g.backend_refund=backend_refund
        self.payment=self.paid()
    def paid(self,**params):
        payment=self.g.handle('/api/pay/create',self.params(**params))
        native=self.payments[('a' if params.get('pid','1')=='1' else 'b',payment['trade_no'])]
        native.update(status='succeeded',amount_received=1000,refunds=[])
        return payment
    def fields(self,**changes):
        p={'pid':'1','trade_no':self.payment['trade_no'],'money':'3.00','out_refund_no':'R1','timestamp':str(int(time.time())),'sign_type':'RSA'}
        p.update(changes)
        p={k:v for k,v in p.items() if v is not None}
        p['sign']=rsa_sign(p,self.root/('a.pem' if p['pid']=='1' else 'b.pem'))
        return p
    def refund(self,**fields):
        return self.g.handle('/api/pay/refund',self.fields(**fields))
    def query(self,**fields):
        defaults={'trade_no':None,'money':None}
        defaults.update(fields)
        return self.g.handle('/api/pay/refundquery',self.fields(**defaults))
class RefundTests(RefundFixture, unittest.TestCase):
    def test_signed_partial_refund_and_query(self):
        result=self.refund()
        self.assertEqual((result['code'],result['status'],result['money']),(0,1,'3.00'))
        self.assertEqual(len(result['refund_no']),30)
        self.assertTrue(rsa_verify({k:str(v) for k,v in result.items()},self.root/'platform.pub'))
        self.assertEqual(self.query()['refund_no'],result['refund_no'])
        self.assertEqual(self.query(out_refund_no=None,refund_no=result['refund_no'])['status'],1)
        self.assertEqual(self.posted[0]['payment_id'],self.payment['trade_no'])
        self.assertEqual(self.posted[0]['amount'],300)
    def test_partial_sum_cannot_exceed_captured_payment(self):
        self.refund()
        self.refund(out_refund_no='R2',money='7.00')
        with self.assertRaises(ProtocolError): self.refund(out_refund_no='R3',money='0.01')
        self.assertEqual(sum(v['amount'] for v in self.refunds.values()),1000)
    def test_idempotent_refund_and_conflicting_reuse(self):
        first=self.refund()
        self.assertEqual(self.refund()['refund_no'],first['refund_no'])
        self.assertEqual(self.refund_creates,1)
        with self.assertRaises(ProtocolError): self.refund(money='4.00')
        other=self.paid(out_trade_no='ORDER_OTHER')
        with self.assertRaises(ProtocolError): self.refund(trade_no=other['trade_no'])
    def test_omitted_refund_number_has_one_stable_default(self):
        first=self.refund(out_refund_no=None)
        self.assertTrue(first['out_refund_no'].startswith('default_'))
        self.assertEqual(self.refund(out_refund_no=None)['refund_no'],first['refund_no'])
        with self.assertRaises(ProtocolError): self.refund(out_refund_no=None,money='4.00')
    def test_unpaid_or_authorized_only_cannot_refund(self):
        for status in ('requires_capture','requires_payment_method','failed'):
            self.payments[('a',self.payment['trade_no'])]['status']=status
            with self.assertRaises(ProtocolError): self.refund()
        self.assertEqual(self.refund_creates,0)
    def test_partial_capture_uses_actual_captured_amount(self):
        self.payments[('a',self.payment['trade_no'])].update(status='partially_captured',amount_received=400)
        with self.assertRaises(ProtocolError): self.refund(money='5.00')
        self.assertEqual(self.refund(money='4.00')['status'],1)
    def test_cross_merchant_refund_and_query_denied(self):
        result=self.refund()
        with self.assertRaises(ProtocolError): self.refund(pid='2')
        with self.assertRaises(ProtocolError): self.query(pid='2',out_refund_no=None,refund_no=result['refund_no'])
        other=self.paid(pid='2')
        second=self.refund(pid='2',trade_no=other['trade_no'])
        self.assertNotEqual(result['refund_no'],second['refund_no'])
        self.assertEqual(self.refund_creates,2)
    def test_concurrent_refunds_never_exceed_captured_amount(self):
        import concurrent.futures
        def refund(i):
            try: return self.refund(out_refund_no='CONCURRENT_'+str(i),money='1.00')['status']==1
            except ProtocolError: return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            results=list(pool.map(refund,range(20)))
        self.assertEqual(sum(results),10)
        self.assertEqual(sum(v['amount'] for v in self.refunds.values()),1000)
    def test_same_refund_in_flight_does_not_submit_twice(self):
        import concurrent.futures
        entered=threading.Event();release=threading.Event()
        def slow(merchant,method,path,row,body=None):
            if method=='POST': entered.set();release.wait(10)
            return self.normal_backend_refund(merchant,method,path,row,body)
        self.g.backend_refund=slow
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.refund)
            try:
                self.assertTrue(entered.wait(5))
                duplicate=self.refund()
                self.assertEqual(duplicate['status'],2)
            finally: release.set()
            self.assertEqual(future.result()['status'],1)
        self.assertEqual(self.refund_creates,1)
    def test_lost_post_response_recovers_existing_native_refund(self):
        def lost(merchant,method,path,row,body=None):
            result=self.normal_backend_refund(merchant,method,path,row,body)
            if method=='POST': raise ProtocolError('Lost reply after native refund')
            return result
        self.g.backend_refund=lost
        self.assertEqual(self.refund()['status'],1)
        self.assertEqual(self.refund()['status'],1)
        self.assertEqual(self.refund_creates,1)
    def test_timeout_before_native_creation_keeps_budget_and_retries_same_id(self):
        attempts=[]
        def lost(merchant,method,path,row,body=None):
            if method=='POST': attempts.append(body['refund_id']);raise ProtocolError('Timeout')
            return None
        self.g.backend_refund=lost
        first=self.refund(money='10.00')
        self.assertEqual((first['code'],first['status']),(-1,2))
        with self.assertRaises(ProtocolError): self.refund(out_refund_no='R2',money='0.01')
        self.g.backend_refund=self.normal_backend_refund
        retry=self.refund(money='10.00')
        self.assertEqual(retry['refund_no'],attempts[0])
        self.assertEqual(retry['status'],1)
        self.assertEqual(self.refund_creates,1)
    def test_pending_never_reported_success_and_query_tracks_completion(self):
        self.native_status='pending'
        created=self.refund()
        self.assertEqual((created['code'],created['status']),(0,2))
        self.assertEqual(self.query()['status'],2)
        self.refunds[('a',created['refund_no'])]['status']='succeeded'
        self.assertEqual(self.query()['status'],1)
        self.assertEqual(self.refund_creates,1)
    def test_native_failure_releases_refundable_budget_but_not_refund_id(self):
        self.native_status='failed'
        failed=self.refund(money='10.00')
        self.assertEqual((failed['code'],failed['status']),(-1,0))
        self.native_status='succeeded'
        self.assertEqual(self.refund(money='10.00')['status'],0)
        self.assertEqual(self.refund(out_refund_no='R2',money='10.00')['status'],1)
    def test_native_failed_without_error_evidence_keeps_budget(self):
        self.native_status='failed'
        def unclear(merchant,method,path,row,body=None):
            result=self.normal_backend_refund(merchant,method,path,row,body)
            if result: result['error_code']=None
            return result
        self.g.backend_refund=unclear
        self.assertEqual(self.refund(money='10.00')['status'],2)
        with self.assertRaises(ProtocolError): self.refund(out_refund_no='R2',money='0.01')
    def test_unknown_transaction_failure_keeps_budget(self):
        self.native_status='transaction_failure'
        first=self.refund(money='10.00')
        self.assertEqual(first['status'],2)
        with self.assertRaises(ProtocolError): self.refund(out_refund_no='R2',money='0.01')
    def test_native_refunds_outside_gateway_count_against_budget(self):
        self.payments[('a',self.payment['trade_no'])]['refunds']=[{'refund_id':'outside','amount':800,'status':'pending'}]
        with self.assertRaises(ProtocolError): self.refund(money='3.00')
        self.assertEqual(self.refund(money='2.00')['status'],1)
    def test_native_refund_binding_cannot_be_substituted(self):
        row={'refund_no':'expected','trade_no':self.payment['trade_no'],'amount':300,'currency':'USD'}
        correct={'refund_id':'expected','payment_id':self.payment['trade_no'],'amount':300,'currency':'USD','profile_id':'profile_a','status':'success'}
        merchant=self.g.merchants['1']
        for changes in ({'payment_id':'other'},{'amount':301},{'currency':'EUR'},{'profile_id':'profile_b'},{'merchant_id':'b'}):
            self.g.backend_json=lambda *args,**kwargs:dict(correct,**changes)
            with self.assertRaises(ProtocolError): Gateway.backend_refund(self.g,merchant,'GET','/refunds/expected',row)
    def test_pid_reassignment_cannot_expose_cached_refunds(self):
        self.refund()
        self.g.merchants['1'].update(merchant_id='b',profile_id='profile_b')
        with self.assertRaises(ProtocolError): self.refund()
        with self.assertRaises(ProtocolError): self.query()
    def test_refund_recovery_after_gateway_restart(self):
        with patch.object(self.g,'record_refund',side_effect=RuntimeError('simulated process crash')):
            with self.assertRaises(RuntimeError): self.refund()
        restarted=Gateway(self.g.config)
        restarted.backend=self.g.backend
        restarted.backend_refund=self.normal_backend_refund
        result=restarted.handle('/api/pay/refundquery',self.fields(trade_no=None,money=None))
        self.assertEqual(result['status'],1)
        self.assertEqual(self.refund_creates,1)
    def test_refund_signature_and_parameter_constraints(self):
        for params in (self.fields(sign_type='MD5'),self.fields(timestamp='1000000000'),self.fields(money='-1'),self.fields(connector='stripe')):
            with self.assertRaises(ProtocolError): self.g.handle('/api/pay/refund',params)
        tampered=self.fields();tampered['money']='4.00'
        with self.assertRaises(ProtocolError): self.g.handle('/api/pay/refund',tampered)
        self.assertEqual(self.refund_creates,0)


class RefundHttpTests(RefundFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server=Server(('127.0.0.1',0),self.g)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client={'api_url':'http://127.0.0.1:'+str(self.server.server_port),'pid':'1','merchant_private_key':str(self.root/'a.pem'),'platform_public_key':str(self.root/'platform.pub')}
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        super().tearDown()
    def test_real_rsa_client_refund_and_refundquery(self):
        from client import request
        result=request(self.client,'refund',{'trade_no':self.payment['trade_no'],'out_refund_no':'HTTP1','money':'3.00'})
        self.assertEqual((result['code'],result['status']),(0,1))
        query=request(self.client,'refundquery',{'refund_no':result['refund_no']})
        self.assertEqual(query['out_refund_no'],'HTTP1')
        self.assertEqual(query['status'],1)
        self.assertEqual(self.refund_creates,1)
    def test_refund_http_duplicate_fields_and_md5_rejected(self):
        for raw in (urllib.parse.urlencode(self.fields(sign_type='MD5')),urllib.parse.urlencode(self.fields())+'&pid=1'):
            req=urllib.request.Request(self.client['api_url']+'/api/pay/refund',data=raw.encode(),headers={'Content-Type':'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(req) as response:
                self.assertNotEqual(json.load(response)['code'],0)
        self.assertEqual(self.refund_creates,0)

if __name__=='__main__': unittest.main()
