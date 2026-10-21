-- Apply to the global user database. The trigger shares the user-role transaction,
-- so a committed account/role change cannot lose its provisioning request.
CREATE TABLE IF NOT EXISTS logto_identity_binding (
    local_user_id VARCHAR(64) NOT NULL REFERENCES users(user_id),
    local_org_id VARCHAR(64),
    local_merchant_id VARCHAR(64),
    logto_subject VARCHAR(128) NOT NULL,
    logto_organization_id VARCHAR(64),
    role_id VARCHAR(64) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'ready' CHECK (status IN ('pending', 'ready', 'disabled')),
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (local_user_id, role_id),
    UNIQUE (logto_subject, role_id),
    CHECK (
        (role_id = 'tenant_admin' AND local_org_id IS NULL AND local_merchant_id IS NULL AND logto_organization_id IS NULL)
        OR
        (role_id IN ('collection_merchant_admin', 'collection_merchant_view_only')
         AND local_org_id IS NOT NULL AND local_merchant_id IS NOT NULL AND logto_organization_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS logto_organization_binding (
    local_org_id VARCHAR(64) PRIMARY KEY,
    logto_organization_id VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logto_sync_outbox (
    id BIGSERIAL PRIMARY KEY,
    source_user_role_id INTEGER NOT NULL UNIQUE,
    local_user_id VARCHAR(64) NOT NULL,
    local_org_id VARCHAR(64),
    local_merchant_id VARCHAR(64),
    role_id VARCHAR(64) NOT NULL,
    role_status VARCHAR(64) NOT NULL,
    generation BIGINT NOT NULL DEFAULT 1,
    state VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'leased', 'done', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT now(),
    lease_until TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS logto_sync_outbox_due ON logto_sync_outbox(next_attempt_at, id)
    WHERE state IN ('pending', 'leased');

CREATE OR REPLACE FUNCTION enqueue_logto_user_role() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (OLD.role_id IS DISTINCT FROM NEW.role_id
        OR OLD.org_id IS DISTINCT FROM NEW.org_id
        OR OLD.merchant_id IS DISTINCT FROM NEW.merchant_id) THEN
        UPDATE logto_identity_binding SET status='disabled', updated_at=now()
        WHERE local_user_id=OLD.user_id AND role_id=OLD.role_id;
    END IF;
    IF NEW.role_id NOT IN ('org_admin', 'tenant_admin', 'collection_merchant_admin', 'collection_merchant_view_only') THEN
        RETURN NEW;
    END IF;
    IF lower(NEW.status) <> 'active' THEN
        UPDATE logto_identity_binding SET status='disabled', updated_at=now()
        WHERE local_user_id=NEW.user_id AND role_id=NEW.role_id;
    END IF;
    INSERT INTO logto_sync_outbox
        (source_user_role_id, local_user_id, local_org_id, local_merchant_id, role_id, role_status)
    VALUES (NEW.id, NEW.user_id, NEW.org_id, NEW.merchant_id, NEW.role_id, NEW.status)
    ON CONFLICT (source_user_role_id) DO UPDATE SET
        local_user_id = EXCLUDED.local_user_id,
        local_org_id = EXCLUDED.local_org_id,
        local_merchant_id = EXCLUDED.local_merchant_id,
        role_id = EXCLUDED.role_id,
        role_status = EXCLUDED.role_status,
        generation = logto_sync_outbox.generation + 1,
        state = 'pending',
        next_attempt_at = now(),
        lease_until = NULL,
        last_error = NULL,
        updated_at = now();
    RETURN NEW;
END;
$$;
CREATE TRIGGER logto_user_role_enqueue
    AFTER INSERT OR UPDATE OF role_id, status, org_id, merchant_id ON user_roles
    FOR EACH ROW EXECUTE FUNCTION enqueue_logto_user_role();

CREATE OR REPLACE FUNCTION refresh_logto_user_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.is_verified AND COALESCE(NEW.is_active, TRUE) THEN
        UPDATE logto_sync_outbox SET state='pending', generation=generation+1,
            next_attempt_at=now(), lease_until=NULL, last_error=NULL, updated_at=now()
        WHERE local_user_id=NEW.user_id AND state IN ('pending', 'failed', 'done');
    ELSE
        UPDATE logto_identity_binding SET status='disabled', updated_at=now()
        WHERE local_user_id=NEW.user_id;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER logto_user_status_refresh
    AFTER UPDATE OF is_verified, is_active ON users
    FOR EACH ROW EXECUTE FUNCTION refresh_logto_user_sync();
