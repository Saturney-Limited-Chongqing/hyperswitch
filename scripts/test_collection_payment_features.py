#!/usr/bin/env python3
"""Remote-only integration: real Hyperswitch DB/API, no real charge requests.
Uses existing collection fixtures; credentials and generated keys stay on the host.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/epay_v2'))
from gateway import Gateway, Server
from client import request

fixtures=json.loads(Path(os.environ.get('COLLECTION_TEST_FIXTURES','/home/flan/Projects/hyperswitch-debug/fixtures.json')).read_text())
base=fixtures['base_url'].rstrip('/')
checks=[]

def native(method,path,headers,body=None):
    req=urllib.request.Request(base+path,method=method,headers=dict(headers,**{'Content-Type':'application/json'}),data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req,timeout=30) as r: return r.status,json.load(r)
    except urllib.error.HTTPError as e:
        try: data=json.load(e)
        except Exception: data={}
        return e.code,data

def check(label,condition):
    assert condition,label
    checks.append(label)

stamp=str(int(time.time()))
a,b=fixtures['merchants']
platform={'Authorization':'Bearer '+a['platform_token'],'X-Merchant-Id':a['merchant_id'],'X-Profile-Id':a['profile_id']}
# Extend only the known disposable Stripe fixtures; keys remain intentionally invalid.
assert all(m['merchant_id'] in ('collection_a','collection_b') for m in fixtures['merchants'])
for m in fixtures['merchants']:
    auth={'Authorization':'Bearer '+m['platform_token']}
    methods={'connector_type':'payment_processor','payment_methods_enabled':[
        {'payment_method':'card','payment_method_types':[{'payment_method_type':'credit','card_networks':['Visa','Mastercard']}]},
        {'payment_method':'wallet','payment_method_types':[{'payment_method_type':'ali_pay','payment_experience':'redirect_to_url'}]}]}
    status,_=native('POST','/account/'+m['merchant_id']+'/connectors/'+m['connector_id'],auth,methods)
    check('disposable channel supports native alipay routing',status==200)
policy={'merchant_id':a['merchant_id'],'merchant_connector_id':a['connector_id'],'pool_id':'http_test_'+stamp,'currency':'USD','timezone':'UTC','daily_limit':0}
# Bindings are immutable, so reuse any existing pool for the same test channel.
status,rows=native('GET','/configs/channel-limits',platform)
check('platform JWT can list quota pools',status==200 and isinstance(rows,list))
for row in rows:
    if row['merchant_id']==a['merchant_id'] and row['merchant_connector_id']==a['connector_id'] and row['currency']=='USD':
        policy['pool_id']=row['pool_id']
        policy['timezone']=row['timezone']
status,data=native('POST','/configs/channel-limits',platform,policy)
check('platform JWT can configure channel limit',status==200)
for merchant in (a,b):
    for headers in ({'api-key':merchant['api_key']},{'Authorization':'Bearer '+merchant['token']}):
        status,_=native('POST','/configs/channel-limits',headers,policy)
        check('merchant quota management denied',status in (401,403))
# A zero quota must reject a confirmed native payment before sending to its upstream.
# Stripe test card data; with zero allowance no connector payment call may occur.
headers={'api-key':a['api_key']}
body={'payment_id':'quota_probe_'+stamp,'amount':100,'currency':'USD','confirm':True,'profile_id':a['profile_id'],
      'authentication_type':'no_three_ds','payment_method':'card','payment_method_type':'credit',
      'payment_method_data':{'card':{'card_number':'4242424242424242','card_exp_month':'12','card_exp_year':'2030','card_cvc':'123','card_holder_name':'Quota Test'}},
      'billing':{'address':{'country':'US','first_name':'Quota','last_name':'Test'},'email':'quota@example.com'}}
status,data=native('POST','/payments',headers,body)
# Do not emit request/response data (contains personal or payment data).
check('native confirmed payment blocked by zero daily quota',status in (400,412) and 'quota' in json.dumps(data).lower())
# Raise the test-only allowance for unconfirmed hosted-link creation. No funds are moved.
policy['daily_limit']=300000
status,_=native('POST','/configs/channel-limits',platform,policy)
check('platform can update daily allowance',status==200)

with tempfile.TemporaryDirectory(prefix='epay-http-') as folder:
    root=Path(folder)
    for name in ('platform','a','b'):
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(root/(name+'.pem'))],capture_output=True,check=True)
        (root/(name+'.pem')).chmod(0o600)
        subprocess.run(['openssl','pkey','-in',str(root/(name+'.pem')),'-pubout','-out',str(root/(name+'.pub'))],capture_output=True,check=True)
    config={'database':str(root/'orders.db'),'platform_private_key':str(root/'platform.pem'),'backend_url':base,'public_url':'http://127.0.0.1:18100',
            'default_return_url':'http://127.0.0.1:19000/home','test_allow_loopback_callbacks':True,'merchants':{}}
    for pid,m,name in [('10001',a,'a'),('10002',b,'b')]:
        config['merchants'][pid]={'merchant_id':m['merchant_id'],'profile_id':m['profile_id'],'api_key':m['api_key'],
                                  'public_keys':[str(root/(name+'.pub'))],'currency':'USD','types':['alipay']}
    gateway=Gateway(config)
    server=Server(('127.0.0.1',0),gateway)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    def client(pid,name):
        return {'api_url':'http://127.0.0.1:'+str(server.server_port),'pid':pid,'merchant_private_key':str(root/(name+'.pem')),'platform_public_key':str(root/'platform.pub')}
    c=client('10001','a')
    params={'out_trade_no':'integration_'+stamp,'money':'1.23','type':'alipay','name':'Integration order','notify_url':'http://127.0.0.1:18101/notify'}
    try:
        one=request(c,'create',params)
        if one.get('code') != 0: print('Gateway create diagnostic:', one.get('msg'))
        check('RSA create yields real backend hosted link',one['code']==0 and bool(one.get('pay_info')))
        two=request(c,'create',params)
        check('RSA retry keeps same native payment',two['code']==0 and two['trade_no']==one['trade_no'])
        status,payment=native('GET','/payments/'+one['trade_no'],{'api-key':a['api_key']})
        check('compat order mapped to correct merchant amount currency',status==200 and payment['merchant_id']==a['merchant_id'] and payment['amount']==123 and payment['currency']=='USD')
        query=request(c,'query',{'trade_no':one['trade_no']})
        check('RSA query awaiting payment',query['code']==0 and query['status']==0)
        other=request(client('10002','b'),'query',{'trade_no':one['trade_no']})
        check('RSA cross merchant query denied',other['code']!=0)
        conflict=request(c,'create',dict(params,money='2.00'))
        check('RSA conflicting duplicate denied',conflict['code']!=0)
        own_b=request(client('10002','b'),'create',params)
        check('same external order number isolated between merchants',own_b['code']==0 and own_b['trade_no']!=one['trade_no'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
print(json.dumps({'passed':len(checks),'checks':checks},indent=2))
