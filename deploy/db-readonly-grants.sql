SELECT set_config('threadwright.dbuser', :'dbuser', false), set_config('threadwright.schemas', :'schemas', false);
DO $$
DECLARE s text;
DECLARE u text := current_setting('threadwright.dbuser');
BEGIN
  FOREACH s IN ARRAY string_to_array(current_setting('threadwright.schemas'), ',') LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = s) THEN
      RAISE NOTICE 'skip % (no such schema)', s; CONTINUE;
    END IF;
    BEGIN
      EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', s, u);
      EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', s, u);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO %I', s, u);
      RAISE NOTICE 'granted SELECT on %', s;
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'FAILED on %: %', s, SQLERRM;
    END;
  END LOOP;
END $$;
SELECT count(*) AS readable_tables FROM information_schema.role_table_grants
 WHERE grantee = :'dbuser' AND privilege_type = 'SELECT';
SELECT count(*) AS write_grants FROM information_schema.role_table_grants
 WHERE grantee = :'dbuser' AND privilege_type <> 'SELECT';
