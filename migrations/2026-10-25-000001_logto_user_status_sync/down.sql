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
