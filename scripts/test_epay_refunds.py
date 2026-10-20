#!/usr/bin/env python3
"""Remote refund contract regression against native router + loopback Stripe simulator.
Creates only synthetic captured fixtures; never contacts a real payment upstream.
Requires the dedicated debug router on 18081, configured from refund-regression.toml.
"""
import concurrent.futures
import http.server
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
DEBUG=Path('/home/flan/Projects/hyperswitch-debug')
sys.path.insert(0,str(ROOT/'services/epay_v2'))
from gateway import Gateway, Server
from client import request

fixtures=json.loads((DEBUG/'fixtures.json').read_text())
config=tomllib.loads((DEBUG/'refund-regression.toml').read_text())
assert config['server']['host']=='127.0.0.1' and config['server']['port']==18081
assert config['connectors']['stripe']['base_url']=='http://127.0.0.1:18102/'
assert {m['merchant_id'] for m in fixtures['merchants']}=={'collection_a','collection_b'}
BASE='http://127.0.0.1:18081'
CHECKS=[]

def check(label,condition):
    assert condition,label
    CHECKS.append(label)

def quote(value): return "'"+str(value).replace("'","''")+"'"
def sql(query):
    result=subprocess.run(['docker','exec','-i','hyperswitch-debug-pg','psql','-X','-qAt','-v','ON_ERROR_STOP=1','-U','db_user','-d','hyperswitch_db'],input=query,text=True,capture_output=True,env=dict(os.environ,DOCKER_HOST='unix:///run/user/1000/docker.sock'))
    assert result.returncode==0,'Synthetic fixture database operation failed'
    return result.stdout.strip()

class Stripe(http.server.BaseHTTPRequestHandler):
    records={}
    run_id=str(time.time_ns())
    creates=0
    next_status='succeeded'
    next_http_status=200
    requests=0
    mutex=threading.Lock()
    def log_message(self,*args): pass
    def do_GET(self): self.dispatch()
    def do_POST(self): self.dispatch()
    def dispatch(self):
        path=urllib.parse.urlsplit(self.path).path
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        with Stripe.mutex:
            if path=='/v1/refunds' and self.command=='POST':
                fields=dict(urllib.parse.parse_qsl(body.decode()))
                payment_intent=fields.get('payment_intent','')
                if not payment_intent.startswith('pi_epay_refund_fixture_'):
                    self.send_error(400);return
                Stripe.requests+=1
                if Stripe.next_http_status>=500:
                    data=json.dumps({'error':{'code':'api_error','type':'api_error','message':'Simulated upstream unavailable'}}).encode()
                    self.send_response(Stripe.next_http_status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
                Stripe.creates+=1
                identifier='re_epay_fixture_'+Stripe.run_id+'_'+str(Stripe.creates)
                result={'id':identifier,'object':'refund','amount':int(fields['amount']),'currency':'usd','status':Stripe.next_status,
                        'payment_intent':payment_intent,'metadata':{'order_id':fields.get('metadata[order_id]'),'is_refund_id_as_reference':'true'}}
                Stripe.records[identifier]=result
            elif path.startswith('/v1/refunds/'):
                result=Stripe.records.get(path.rsplit('/',1)[-1])
                if not result: self.send_error(404);return
            else:
                self.send_error(404);return
            data=json.dumps(result).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

stripe=http.server.ThreadingHTTPServer(('127.0.0.1',18102),Stripe)
stripe_thread=threading.Thread(target=stripe.serve_forever,daemon=True);stripe_thread.start()
try:
 with tempfile.TemporaryDirectory(prefix='epay-refund-integration-') as folder:
    root=Path(folder)
    for name in ('platform','a','b'):
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(root/(name+'.pem'))],capture_output=True,check=True)
        (root/(name+'.pem')).chmod(0o600)
        subprocess.run(['openssl','pkey','-in',str(root/(name+'.pem')),'-pubout','-out',str(root/(name+'.pub'))],capture_output=True,check=True)
    conf={'database':str(root/'orders.db'),'platform_private_key':str(root/'platform.pem'),'backend_url':BASE,
          'public_url':'http://127.0.0.1:18104','default_return_url':'http://127.0.0.1:19000/home','test_allow_loopback_callbacks':True,'merchants':{}}
    for pid,merchant,name in zip(('10001','10002'),fixtures['merchants'],('a','b')):
        conf['merchants'][pid]={'merchant_id':merchant['merchant_id'],'profile_id':merchant['profile_id'],'api_key':merchant['api_key'],
                              'public_keys':[str(root/(name+'.pub'))],'currency':'USD','types':['alipay']}
    gateway=Gateway(conf)
    server=Server(('127.0.0.1',0),gateway)
    gateway.public_url='http://127.0.0.1:'+str(server.server_port)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def client(pid):
        return {'api_url':gateway.public_url,'pid':pid,'merchant_private_key':str(root/('a.pem' if pid=='10001' else 'b.pem')),'platform_public_key':str(root/'platform.pub')}
    stamp=str(time.time_ns())
    def create(label,pid='10001',captured=True):
        external='refund_fixture_'+stamp+'_'+label
        result=request(client(pid),'create',{'type':'alipay','out_trade_no':external,'name':'Synthetic refund fixture','money':'10.00','notify_url':'http://127.0.0.1:18101/notify'})
        check('native unconfirmed refund fixture created',result['code']==0)
        trade=result['trade_no']
        merchant=fixtures['merchants'][0 if pid=='10001' else 1]
        if captured:
            predicate='merchant_id='+quote(merchant['merchant_id'])+' AND payment_id='+quote(trade)
            sql("UPDATE payment_intent SET status='succeeded',amount_captured=1000 WHERE "+predicate+"; UPDATE payment_attempt SET status='charged',connector='stripe',merchant_connector_id="+quote(merchant['connector_id'])+",payment_method='wallet',payment_method_type='ali_pay',connector_transaction_id="+quote('pi_epay_refund_fixture_'+label+'_'+stamp)+" WHERE "+predicate+';')
            attempt=sql('SELECT attempt_id FROM payment_attempt WHERE '+predicate+' ORDER BY created_at DESC LIMIT 1;')
            payload={'merchant_id':merchant['merchant_id'],'merchant_connector_id':merchant['connector_id'],'attempt_id':attempt,'currency':'USD','amount':1000}
            reserve=json.loads(sql("SELECT channel_limit_command('reserve',"+quote(json.dumps(payload))+"::jsonb);"))
            assert reserve['allowed'],'Synthetic quota reservation failed'
            payload['state']='committed'
            sql("SELECT channel_limit_command('settle',"+quote(json.dumps(payload))+"::jsonb);")
        return trade
    def usage():
        rows=json.loads(sql("SELECT channel_limit_command('list','{}'::jsonb);"))
        return {(row['merchant_id'],row['merchant_connector_id']):row['used'] for row in rows}
    def refund(trade,external,money,pid='10001'):
        return request(client(pid),'refund',{'trade_no':trade,'out_refund_no':external,'money':money})
    try:
        trade=create('partial')
        before=usage()
        one=refund(trade,'R_'+stamp,'3.00')
        if one['code']!=0: print('Refund diagnostic:',one.get('msg'),one.get('refund_status'))
        check('RSA refund reaches native Stripe connector and succeeds',one['code']==0 and one.get('status')==1)
        check('native provider received correct partial amount',list(Stripe.records.values())[0]['amount']==300)
        count=Stripe.creates
        duplicate=refund(trade,'R_'+stamp,'3.00')
        check('repeat refund has one provider side effect',duplicate['refund_no']==one['refund_no'] and Stripe.creates==count)
        query=request(client('10001'),'refundquery',{'refund_no':one['refund_no']})
        check('signed native refund query succeeds',query['code']==0 and query['status']==1)
        check('cross merchant refund query refused',request(client('10002'),'refundquery',{'refund_no':one['refund_no']})['code']!=0)
        check('cross merchant refund creation refused',refund(trade,'CROSS_'+stamp,'1.00','10002')['code']!=0)
        check('refund number amount conflict refused',refund(trade,'R_'+stamp,'4.00')['code']!=0)
        second=refund(trade,'R2_'+stamp,'7.00')
        check('second partial refund completes captured amount',second['code']==0 and second['status']==1)
        count=Stripe.creates
        check('excess cumulative refund rejected before upstream',refund(trade,'R3_'+stamp,'0.01')['code']!=0 and Stripe.creates==count)
        check('refunds never restore daily channel allowance',usage()==before)
        unpaid=create('unpaid',captured=False)
        check('unpaid order cannot trigger provider refund',refund(unpaid,'UNPAID_'+stamp,'1.00')['code']!=0 and Stripe.creates==count)
        other=create('merchant_b','10002')
        check('merchant B can refund its own order',refund(other,'R_'+stamp,'2.00','10002')['status']==1)
        pending=create('pending')
        Stripe.next_status='pending'
        p=refund(pending,'P_'+stamp,'4.00')
        check('native pending refund is not successful',p['code']==0 and p['status']==2)
        # Simulate provider completion; force_sync must update the real native refund.
        latest=list(Stripe.records.values())[-1];latest['status']='succeeded'
        check('native refund sync observes later provider success',request(client('10001'),'refundquery',{'out_refund_no':'P_'+stamp})['status']==1)
        Stripe.next_status='succeeded'
        parallel_trade=create('concurrent')
        def attempt(i): return refund(parallel_trade,'C_'+stamp+'_'+str(i),'2.00')
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            outcomes=list(pool.map(attempt,range(10)))
        check('concurrent native refunds stay within captured total',sum(r.get('status')==1 for r in outcomes)==5)
        check('concurrent rejections do not over-refund upstream',sum(v['amount'] for v in Stripe.records.values() if v['payment_intent'].startswith('pi_epay_refund_fixture_concurrent_'))==1000)
        declined=create('declined')
        Stripe.next_status='failed'
        rejected=refund(declined,'DECLINED_'+stamp,'10.00')
        check('explicit provider refund decline is not success',rejected['code']!=0 and rejected['status']==0)
        Stripe.next_status='succeeded'
        check('confirmed decline permits a new refund request',refund(declined,'DECLINED_RETRY_'+stamp,'10.00')['status']==1)
        uncertain=create('upstream_5xx')
        Stripe.next_http_status=500
        waiting=refund(uncertain,'UNCERTAIN_'+stamp,'10.00')
        check('native upstream 5xx remains pending',waiting['status']==2)
        before_requests=Stripe.requests
        check('pending refund retry does not resubmit upstream',refund(uncertain,'UNCERTAIN_'+stamp,'10.00')['status']==2 and Stripe.requests==before_requests)
        check('uncertain refund amount cannot be reused',refund(uncertain,'UNCERTAIN_NEW_'+stamp,'0.01')['code']!=0 and Stripe.requests==before_requests)
        Stripe.next_http_status=200
        print(json.dumps({'passed':len(CHECKS),'checks':CHECKS,'simulated_provider_refunds':Stripe.creates,'real_upstream_calls':0},indent=2))
    finally:
        server.shutdown();server.server_close();thread.join()
finally:
    stripe.shutdown();stripe.server_close();stripe_thread.join()
