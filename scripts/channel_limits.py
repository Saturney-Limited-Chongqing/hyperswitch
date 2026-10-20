#!/usr/bin/env python3
"""Platform channel-limit management. Credentials come from environment, never arguments."""
import argparse
import json
import os
import urllib.request

parser=argparse.ArgumentParser()
parser.add_argument('action',choices=['list','set','resolve'])
parser.add_argument('--policy',help='JSON policy file for set')
args=parser.parse_args()
headers={'Content-Type':'application/json'}
if os.environ.get('HYPERSWITCH_PLATFORM_TOKEN'):
    headers['Authorization']='Bearer '+os.environ['HYPERSWITCH_PLATFORM_TOKEN']
else:
    headers['api-key']=os.environ['HYPERSWITCH_ADMIN_KEY']
body=None
if args.action in ('set','resolve'):
    with open(args.policy) as f: body=json.dumps(json.load(f)).encode()
req=urllib.request.Request(os.environ.get('HYPERSWITCH_API_URL','http://127.0.0.1:18080').rstrip('/')+'/configs/channel-limits'+('/reconcile' if args.action=='resolve' else ''),data=body,headers=headers)
with urllib.request.urlopen(req,timeout=20) as r:
    print(json.dumps(json.load(r),indent=2))
