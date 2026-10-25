-- Requeue every managed Logto role when the source Hyperswitch user is disabled
-- or re-enabled. The worker uses the event to suspend/reactivate the managed
-- Logto identity. Apply this to the global user database.
CREATE OR REPLACE FUNCTION refresh_logto_user_sync() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT NEW.is_verified OR NOT COALESCE(NEW.is_active, TRUE) THEN
        UPDATE logto_identity_binding SET status='disabled', updated_at=now()
        WHERE local_user_id=NEW.user_id;
    END IF;

    UPDATE logto_sync_outbox SET state='pending', generation=generation+1,
        next_attempt_at=now(), lease_until=NULL, last_error=NULL, updated_at=now()
    WHERE local_user_id=NEW.user_id;
    RETURN NEW;
END;
$$;
