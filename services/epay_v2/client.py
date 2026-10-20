#!/usr/bin/env python3
"""Minimal RSA V2 client; no MD5 or shared-key authentication."""
import argparse
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request
from gateway import rsa_sign, rsa_verify


def request(config, endpoint, params):
    if endpoint not in ('create', 'query'):
        raise ValueError('Unsupported endpoint')
    parsed = urllib.parse.urlsplit(config['api_url'])
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('HTTPS required')
    fields = {k: str(v) for k, v in params.items()}
    fields.update(pid=str(config['pid']), timestamp=str(int(time.time())), sign_type='RSA')
    fields['sign'] = rsa_sign(fields, config['merchant_private_key'])
    req = urllib.request.Request(config['api_url'].rstrip('/') + '/api/pay/' + endpoint,
                                 data=urllib.parse.urlencode(fields).encode(), headers={'Content-Type':'application/x-www-form-urlencoded'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(req, timeout=30) as response:
        result = json.loads(response.read(65536))
    if result.get('sign_type') != 'RSA' or abs(int(time.time()) - int(result.get('timestamp',0))) > 300:
        raise ValueError('Invalid response signature type or timestamp')
    if not rsa_verify({k:str(v) for k,v in result.items()},config['platform_public_key']):
        raise ValueError('Invalid platform signature')
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('endpoint',choices=['create','query'])
    parser.add_argument('params',help='JSON object with order fields')
    args=parser.parse_args()
    path=Path(args.config)
    if path.stat().st_mode & 0o077:
        raise SystemExit('Client configuration must have mode 0600')
    config=json.loads(path.read_text())
    if Path(config['merchant_private_key']).stat().st_mode & 0o077:
        raise SystemExit('Merchant private key must have mode 0600')
    print(json.dumps(request(config,args.endpoint,json.loads(args.params)),ensure_ascii=False))

if __name__=='__main__': main()
