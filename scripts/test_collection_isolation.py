"""HTTP isolation checks against a running backend and provisioned fixture file."""
import json
import os
import pathlib
import time
import bootstrap_collection_test as api
call = api.call
fixture_path = pathlib.Path(os.environ.get('COLLECTION_TEST_FIXTURES', '/private/tmp/collection-test-fixtures.json'))
fixtures = json.loads(fixture_path.read_text())
api.BASE = fixtures['base_url']
a, b = fixtures['merchants']
checks = []

def auth(m, service=False):
    return {**({'api-key': m['api_key']} if service else {'Authorization': 'Bearer ' + m['token']}), 'X-Merchant-Id': m['merchant_id'], 'X-Profile-Id': m['profile_id']}

def success(label, method, path, body=None, headers=None):
    status, result = call(method, path, body, headers)
    assert status in (200, 201), (label, status, result)
    checks.append(label)
    return result

def denied(label, method, path, body=None, headers=None, not_found=False):
    status, result = call(method, path, body, headers)
    allowed = (400, 401, 403, 404) if not_found else (400, 401, 403)
    assert status in allowed, (label, status, result)
    checks.append(label)
stamp = str(int(time.time()))
for merchant, label in ((a, 'A'), (b, 'B')):
    acl = success(f'{label} role discovery', 'GET', '/user/role/v2', headers=auth(merchant))
    assert not {'connector', 'routing', 'clone_connector', 'payout'} & set(acl['resources']), acl
    members = success(f'{label} merchant membership list', 'GET', '/user/list/merchant', headers=auth(merchant))
    assert {m['merchant_id'] for m in members} == {merchant['merchant_id']}, members
    success(f'{label} platform administrator channel access', 'GET', f"/account/{merchant['merchant_id']}/connectors", headers={**auth(merchant), 'Authorization': 'Bearer ' + merchant['platform_token']})
    for service in (False, True):
        denied(f'{label} channel access rejected (service={service})', 'GET', f"/account/{merchant['merchant_id']}/connectors", headers=auth(merchant, service))
    denied(f'{label} custom privilege escalation rejected', 'POST', '/user/role', {'role_name': 'Channel owner', 'groups': ['connectors_manage'], 'role_scope': 'merchant', 'entity_type': 'merchant'}, auth(merchant))
    denied(f'{label} request routing override rejected', 'POST', '/payments', {'amount': 100, 'currency': 'USD', 'confirm': False, 'profile_id': merchant['profile_id'], 'routing': {'type': 'single', 'data': {'connector': 'stripe'}}}, auth(merchant, True))
    invited = success(f'{label} role invitation evaluated', 'POST', '/user/user/invite_multiple', [{'email': f'escalate-{label}-{stamp}@test.com', 'name': 'Escalation test', 'role_id': 'merchant_admin'}], auth(merchant))
    assert invited[0].get('error') and (not invited[0].get('password')), invited
    shared_id = 'shared_customer_' + stamp
    success(f'{label} create same-named customer', 'POST', '/customers', {'customer_id': shared_id, 'name': f'Merchant {label} private customer', 'email': f'{label}-{stamp}@test.com'}, auth(merchant, True))
    own = success(f'{label} retrieve own same-named customer', 'GET', f'/customers/{shared_id}', headers=auth(merchant, True))
    assert own['name'] == f'Merchant {label} private customer', own
    unique = f'private_{label}_{stamp}'
    success(f'{label} create private customer', 'POST', '/customers', {'customer_id': unique, 'name': f'Private {label}'}, auth(merchant, True))
    payment = success(f'{label} create collection order', 'POST', '/payments', {'payment_id': f'payment_{label}_{stamp}', 'amount': 100 if label == 'A' else 200, 'currency': 'USD', 'confirm': False, 'profile_id': merchant['profile_id']}, auth(merchant, True))
    assert payment['merchant_id'] == merchant['merchant_id'], payment
for merchant, other, label in ((a, b, 'A'), (b, a, 'B')):
    other_label = 'B' if label == 'A' else 'A'
    denied(f'{label} cannot switch to other merchant', 'POST', '/user/switch/merchant', {'merchant_id': other['merchant_id']}, auth(merchant), not_found=True)
    denied(f'{label} cannot read other account', 'GET', f"/accounts/{other['merchant_id']}", headers=auth(merchant), not_found=True)
    denied(f'{label} cannot read other customer', 'GET', f'/customers/private_{other_label}_{stamp}', headers=auth(merchant, True), not_found=True)
    denied(f'{label} cannot read other order', 'GET', f'/payments/payment_{other_label}_{stamp}', headers=auth(merchant, True), not_found=True)
    denied(f'{label} cannot create key for other merchant', 'POST', f"/api_keys/{other['merchant_id']}", {'name': 'Forbidden cross-merchant key', 'expiration': 'never'}, auth(merchant), not_found=True)
print(json.dumps({'passed': len(checks), 'checks': checks}, indent=2))
