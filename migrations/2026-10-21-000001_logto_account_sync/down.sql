DROP TRIGGER IF EXISTS logto_user_status_refresh ON users;
DROP FUNCTION IF EXISTS refresh_logto_user_sync();
DROP TRIGGER IF EXISTS logto_user_role_enqueue ON user_roles;
DROP FUNCTION IF EXISTS enqueue_logto_user_role();
DROP TABLE IF EXISTS logto_sync_outbox;
DROP TABLE IF EXISTS logto_organization_binding;
DROP TABLE IF EXISTS logto_identity_binding;
