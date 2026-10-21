import unittest

from logto_account_sync import Database, Logto, sql_value


class FakeDatabase:
    def __init__(self, user):
        self.record = user
        self.finished = []
        self.org = None

    def user(self, _):
        return self.record

    def org_id(self, _):
        return self.org

    def save_org(self, _, remote):
        self.org = remote

    def finish(self, item, subject, org):
        self.finished.append((item['id'], subject, org))

    def finish_unprivileged(self, item):
        self.finished.append((item['id'], 'unprivileged', None))

    def defer_manual(self, item):
        self.finished.append((item['id'], 'manual', None))


class FakeLogto(Logto):
    def __init__(self):
        self.user = None
        self.organization = None
        self.calls = []

    def call(self, method, path, body=None, allow_missing=False):
        self.calls.append((method, path, body))
        if path.startswith('/api/users/') and method == 'GET':
            return self.user
        if path == '/api/users' and method == 'POST':
            self.user = {'id': body['id'], 'primaryEmail': body['primaryEmail'],
                         'customData': body['customData']}
            return self.user
        if path.startswith('/api/organizations?page='):
            return [self.organization] if self.organization else []
        if path == '/api/organizations' and method == 'POST':
            self.organization = {'id': 'org_logto', 'customData': body['customData']}
            return self.organization
        if path == '/api/organizations/org_logto' and method == 'GET':
            return self.organization
        return None


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase({'user_id':'hs_user', 'email':'merchant@example.com',
                                'name':'Merchant', 'is_verified':True, 'is_active':True})
        self.logto = FakeLogto()
        self.item = {'id':1, 'local_user_id':'hs_user', 'local_org_id':'hs_org',
                     'local_merchant_id':'hs_merchant', 'role_id':'collection_merchant_admin',
                     'role_status':'active'}

    def test_provision_is_idempotent_and_never_copies_password(self):
        self.logto.provision(self.db, self.item)
        self.logto.provision(self.db, self.item)
        self.assertEqual(sum(method == 'POST' and path == '/api/users'
                             for method, path, _ in self.logto.calls), 1)
        self.assertEqual(sum(method == 'POST' and path == '/api/organizations'
                             for method, path, _ in self.logto.calls), 1)
        self.assertEqual(self.db.finished[-1], (1, 'hs_user', 'org_logto'))
        self.assertNotIn('password', str(self.logto.calls).lower())

    def test_existing_email_with_wrong_id_cannot_be_claimed(self):
        self.logto.user = {'id':'other_user', 'primaryEmail':'merchant@example.com',
                           'customData':{'hyperswitch_user_id':'other_user'}}
        with self.assertRaises(RuntimeError):
            self.logto.provision(self.db, self.item)
        self.assertEqual(self.db.finished, [])

    def test_unverified_user_is_retried_and_platform_role_requires_approval(self):
        self.db.record['is_verified'] = False
        with self.assertRaises(RuntimeError):
            self.logto.provision(self.db, self.item)
        self.db.record['is_verified'] = True
        self.item['role_id'] = 'tenant_admin'
        self.logto.provision(self.db, self.item)
        self.assertEqual(self.db.finished, [(1, 'manual', None)])

    def test_self_signup_only_creates_unprivileged_identity(self):
        self.item['role_id'] = 'org_admin'
        self.logto.provision(self.db, self.item)
        self.assertEqual(self.db.finished, [(1, 'unprivileged', None)])
        self.assertFalse(any(path.endswith('/roles') for _, path, _ in self.logto.calls))

    def test_sql_value_is_encoded_not_interpolated(self):
        value = sql_value("x'); DROP TABLE users; --")
        self.assertNotIn('DROP TABLE', value)
        self.assertTrue(value.startswith("convert_from(decode('"))


if __name__ == '__main__':
    unittest.main()
