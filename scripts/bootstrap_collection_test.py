"""Provision disposable platform/merchant fixtures; never point this at production."""
import json, urllib.request, urllib.error, pathlib, os, argparse
BASE = os.environ.get('COLLECTION_TEST_API_URL', 'http://127.0.0.1:18080')
ADMIN = {'api-key': os.environ.get('COLLECTION_TEST_ADMIN_KEY', 'test_admin')}
PASSWORD = 'IsolationTest2026!'
EMAIL_TAG = os.environ.get('COLLECTION_TEST_EMAIL_TAG', '')
EMAIL_DOMAIN = os.environ.get('COLLECTION_TEST_EMAIL_DOMAIN', 'test.com')


def test_email(name):
    suffix = f'-{EMAIL_TAG}' if EMAIL_TAG else ''
    return f'{name}{suffix}@{EMAIL_DOMAIN}'

def call(method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
            value = json.loads(raw) if raw else None
            return (res.status, value)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        return (e.code, value)

def ok(method, path, body=None, headers=None):
    status, result = call(method, path, body, headers)
    assert status in (200, 201), (method, path, status, result)
    return result

def login(email, password):
    response = ok('POST', '/user/signin', {'email': email, 'password': password})
    for _ in range(5):
        typ = response.get('token_type', '')
        if typ == 'user_info':
            return (response['token'], password)
        auth = {'Authorization': 'Bearer ' + response['token']}
        if typ == 'totp':
            response = ok('GET', '/user/2fa/terminate?skip_two_factor_auth=true', headers=auth)
        elif typ == 'force_set_password':
            ok('POST', '/user/rotate_password', {'password': PASSWORD}, auth)
            password = PASSWORD
            response = ok('POST', '/user/signin', {'email': email, 'password': password})
        else:
            raise AssertionError(('unexpected auth flow', typ))
    raise AssertionError('login flow did not terminate')
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Provision disposable local collection-isolation fixtures')
    parser.add_argument('--provision-disposable-database', action='store_true', required=True, help='Confirm the target API uses a disposable test database')
    parser.parse_args()
    existing_status, existing = call('GET', '/accounts/collection_a', headers=ADMIN)
    if existing_status == 200:
        orgid = existing['organization_id']
    else:
        org = ok('POST', '/organization', {'organization_name': 'Collection Isolation Test'}, ADMIN)
        orgid = org['organization_id']
    merchants = []
    for suffix in ('a', 'b'):
        mid = 'collection_' + suffix
        status, account = call('GET', f'/accounts/{mid}', headers=ADMIN)
        if status != 200:
            account = ok('POST', '/accounts', {'merchant_id': mid, 'merchant_name': 'Collection ' + suffix.upper(), 'organization_id': orgid, 'primary_business_details': [{'country': 'US', 'business': 'default'}]}, ADMIN)
        assert account['organization_id'] == orgid

        pid = account['default_profile']
        key = ok('POST', f'/api_keys/{mid}', {'name': 'Isolation integration key', 'expiration': 'never'}, ADMIN)
        merchants.append({'merchant_id': mid, 'profile_id': pid, 'api_key': key['api_key']})
    platform_email = test_email('platform-isolation')
    status, result = call('POST', '/user/tenant_signup', {'name': 'Platform Test Admin', 'email': platform_email, 'password': PASSWORD}, ADMIN)
    assert status in (200, 201) or 'exists' in str(result).lower(), (status, result)
    platform_token, _ = login(platform_email, PASSWORD)
    platform_info = ok('GET', '/user', headers={'Authorization': 'Bearer ' + platform_token})
    for m, suffix in zip(merchants, ('a', 'b')):
        switched = {'token': platform_token} if platform_info.get('merchant_id') == m['merchant_id'] else ok('POST', '/user/switch/merchant', {'merchant_id': m['merchant_id']}, {'Authorization': 'Bearer ' + platform_token})
        m['platform_token'] = switched['token']
        channels = ok('GET', f"/account/{m['merchant_id']}/connectors", headers={'Authorization': 'Bearer ' + m['platform_token']})
        matches = [c for c in channels if c.get('connector_label') == 'isolation_stripe_' + suffix]
        if matches:
            channel = matches[0]
        else:
            channel = ok('POST', f"/account/{m['merchant_id']}/connectors", {'connector_type': 'payment_processor', 'connector_name': 'stripe', 'connector_account_details': {'auth_type': 'HeaderKey', 'api_key': 'sk_test_collection_isolation'}, 'profile_id': m['profile_id'], 'test_mode': True, 'disabled': False, 'connector_label': 'isolation_stripe_' + suffix, 'payment_methods_enabled': [{'payment_method': 'card', 'payment_method_types': [{'payment_method_type': 'credit', 'card_networks': ['Visa', 'Mastercard']}]}]}, {'Authorization': 'Bearer ' + m['platform_token']})

        m['connector_id'] = channel.get('merchant_connector_id', channel.get('id'))
        email = test_email(f'collection-{suffix}-isolation')
        invited = ok('POST', '/user/user/invite_multiple', [{'email': email, 'name': 'Collection ' + suffix.upper(), 'role_id': 'collection_merchant_admin'}], {'Authorization': 'Bearer ' + m['platform_token']})
        if invited[0].get('password'):
            initial_password = invited[0]['password']
        else:
            assert 'already' in str(invited[0]).lower(), invited
            initial_password = PASSWORD
        token, password = login(email, initial_password)
        m.update(email=email, password=password, token=token)
    fixture = {'base_url': BASE, 'organization_id': orgid, 'platform_email': platform_email, 'password': PASSWORD, 'merchants': merchants}
    target = pathlib.Path(os.environ.get('COLLECTION_TEST_FIXTURES', '/private/tmp/collection-test-fixtures.json'))
    target.write_text(json.dumps(fixture, indent=2))
    target.chmod(384)
    print('Provisioned platform administrator and two collection merchants in one organization')
