-- Per-tenant schema. A pool may be shared by explicitly mapped merchant channel accounts.
CREATE TABLE channel_limit_pool (
 pool_id TEXT PRIMARY KEY, currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
 daily_limit BIGINT NOT NULL CHECK (daily_limit >= 0), timezone TEXT NOT NULL DEFAULT 'UTC',
 enabled BOOLEAN NOT NULL DEFAULT TRUE, modified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE channel_limit_binding (
 merchant_id TEXT NOT NULL, merchant_connector_id TEXT NOT NULL, currency TEXT NOT NULL,
 pool_id TEXT NOT NULL REFERENCES channel_limit_pool(pool_id),
 PRIMARY KEY (merchant_id, merchant_connector_id, currency)
);
CREATE TABLE channel_limit_reservation (
 merchant_id TEXT NOT NULL, attempt_id TEXT NOT NULL, merchant_connector_id TEXT NOT NULL,
 pool_id TEXT NOT NULL REFERENCES channel_limit_pool(pool_id), bucket DATE NOT NULL,
 amount BIGINT NOT NULL CHECK (amount > 0), state TEXT NOT NULL CHECK (state IN ('reserved','committed','released')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), modified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY (merchant_id, attempt_id, merchant_connector_id)
);
CREATE INDEX channel_limit_usage ON channel_limit_reservation(pool_id,bucket) WHERE state <> 'released';
CREATE INDEX channel_limit_pending ON channel_limit_reservation(pool_id) WHERE state='reserved';
CREATE TABLE channel_limit_audit (
 id BIGSERIAL PRIMARY KEY, operation TEXT NOT NULL, payload JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE FUNCTION channel_limit_command(op TEXT, p JSONB) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE pool channel_limit_pool; prior channel_limit_reservation; bucket_day DATE; usage NUMERIC; requested BIGINT; result JSONB; pool_key TEXT;
BEGIN
 IF op = 'set' THEN
  -- Validate timezone and ensure channel belongs to the supplied merchant.
  IF NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name=p->>'timezone') THEN RAISE EXCEPTION 'invalid timezone'; END IF;
  IF NOT EXISTS (SELECT 1 FROM merchant_connector_account WHERE merchant_id=p->>'merchant_id' AND merchant_connector_id=p->>'merchant_connector_id') THEN RAISE EXCEPTION 'unknown merchant channel'; END IF;
  IF (p->>'daily_limit')::BIGINT < 0 THEN RAISE EXCEPTION 'negative limit'; END IF;
  pool_key := p->>'pool_id';
  INSERT INTO channel_limit_pool(pool_id,currency,daily_limit,timezone,enabled)
   VALUES(pool_key,p->>'currency',(p->>'daily_limit')::BIGINT,p->>'timezone',COALESCE((p->>'enabled')::BOOLEAN,TRUE))
   ON CONFLICT(pool_id) DO UPDATE SET daily_limit=EXCLUDED.daily_limit, enabled=EXCLUDED.enabled,modified_at=now();
  SELECT * INTO pool FROM channel_limit_pool WHERE pool_id=pool_key FOR UPDATE;
  -- Changing bucket semantics or currency would discard existing usage. Use a new pool only after reconciliation.
  IF pool.currency <> p->>'currency' OR pool.timezone <> p->>'timezone' THEN RAISE EXCEPTION 'pool currency/timezone immutable'; END IF;
  IF EXISTS (SELECT 1 FROM channel_limit_binding WHERE merchant_id=p->>'merchant_id' AND merchant_connector_id=p->>'merchant_connector_id' AND currency=p->>'currency' AND pool_id<>pool_key) THEN RAISE EXCEPTION 'channel pool binding immutable'; END IF;
  INSERT INTO channel_limit_binding VALUES(p->>'merchant_id',p->>'merchant_connector_id',p->>'currency',pool_key) ON CONFLICT DO NOTHING;
  IF EXISTS (SELECT 1 FROM channel_limit_binding WHERE merchant_id=p->>'merchant_id' AND merchant_connector_id=p->>'merchant_connector_id' AND currency=p->>'currency' AND pool_id<>pool_key) THEN RAISE EXCEPTION 'concurrent binding conflict'; END IF;
  INSERT INTO channel_limit_audit(operation,payload) VALUES(op,p);
  RETURN to_jsonb(pool);
 ELSIF op = 'list' THEN
  SELECT COALESCE(jsonb_agg(to_jsonb(b)||to_jsonb(c)||jsonb_build_object('used',COALESCE((SELECT sum(r.amount) FROM channel_limit_reservation r WHERE r.pool_id=c.pool_id AND (r.state='reserved' OR (r.state='committed' AND r.bucket=(now() AT TIME ZONE c.timezone)::date))),0))), '[]'::jsonb)
  INTO result FROM channel_limit_binding b JOIN channel_limit_pool c USING(pool_id);
  RETURN result;
 ELSIF op IN ('reserve','check') THEN
  SELECT c.* INTO pool FROM channel_limit_binding b JOIN channel_limit_pool c USING(pool_id)
   WHERE b.merchant_id=p->>'merchant_id' AND b.merchant_connector_id=p->>'merchant_connector_id' AND b.currency=p->>'currency' FOR UPDATE OF c;
  IF NOT FOUND THEN
   -- Once a channel is managed, unconfigured currencies cannot bypass its limits.
   RETURN jsonb_build_object('allowed',NOT EXISTS(SELECT 1 FROM channel_limit_binding WHERE merchant_id=p->>'merchant_id' AND merchant_connector_id=p->>'merchant_connector_id'),'managed',FALSE);
  END IF;
  IF NOT pool.enabled THEN RETURN jsonb_build_object('allowed',FALSE,'reason','channel_disabled'); END IF;
  requested := (p->>'amount')::BIGINT;
  IF requested = 0 THEN RETURN jsonb_build_object('allowed',TRUE,'managed',TRUE); END IF;
  IF requested < 0 THEN RETURN jsonb_build_object('allowed',FALSE,'reason','invalid_amount'); END IF;
  SELECT * INTO prior FROM channel_limit_reservation WHERE merchant_id=p->>'merchant_id' AND attempt_id=p->>'attempt_id' AND merchant_connector_id=p->>'merchant_connector_id';
  IF FOUND THEN
   RETURN jsonb_build_object('allowed',prior.state<>'released' AND prior.amount=requested AND prior.pool_id=pool.pool_id AND (prior.state='reserved' OR prior.bucket=(now() AT TIME ZONE pool.timezone)::date),'managed',TRUE,'reused',TRUE);
  END IF;
  bucket_day := (now() AT TIME ZONE pool.timezone)::date;
  SELECT COALESCE(sum(amount),0) INTO usage FROM channel_limit_reservation WHERE pool_id=pool.pool_id AND (state='reserved' OR (state='committed' AND bucket=bucket_day));
  IF usage+requested > pool.daily_limit THEN RETURN jsonb_build_object('allowed',FALSE,'reason','daily_limit_exceeded'); END IF;
  IF op = 'check' THEN RETURN jsonb_build_object('allowed',TRUE,'managed',TRUE); END IF;
  INSERT INTO channel_limit_reservation(merchant_id,attempt_id,merchant_connector_id,pool_id,bucket,amount,state)
   VALUES(p->>'merchant_id',p->>'attempt_id',p->>'merchant_connector_id',pool.pool_id,bucket_day,requested,'reserved');
  RETURN jsonb_build_object('allowed',TRUE,'managed',TRUE,'bucket',bucket_day);
 ELSIF op IN ('settle','resolve') THEN
  IF op='resolve' AND (length(trim(COALESCE(p->>'reason','')))<10 OR COALESCE(p->>'state','') NOT IN ('committed','released')) THEN RAISE EXCEPTION 'resolution requires state and reconciliation evidence'; END IF;
  SELECT c.* INTO pool FROM channel_limit_reservation r JOIN channel_limit_pool c USING(pool_id)
   WHERE r.merchant_id=p->>'merchant_id' AND r.attempt_id=p->>'attempt_id' AND r.merchant_connector_id=p->>'merchant_connector_id' FOR UPDATE OF c;
  IF NOT FOUND THEN
   IF op='resolve' THEN RAISE EXCEPTION 'reservation not found'; END IF;
   RETURN '{}'::jsonb;
  END IF;
  IF op='resolve' THEN INSERT INTO channel_limit_audit(operation,payload) VALUES(op,p); END IF;
  -- A committed charge is never restored by a later refund or failed capture.
  UPDATE channel_limit_reservation SET state=p->>'state',bucket=CASE WHEN p->>'state'='committed' THEN (now() AT TIME ZONE pool.timezone)::date ELSE bucket END,modified_at=now()
   WHERE merchant_id=p->>'merchant_id' AND attempt_id=p->>'attempt_id' AND merchant_connector_id=p->>'merchant_connector_id' AND state='reserved'
   AND p->>'state' IN ('committed','released');
  RETURN '{}'::jsonb;
 END IF;
 RAISE EXCEPTION 'invalid channel limit operation';
END $$;
