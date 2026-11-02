-- =====================================================================
-- 운영 PostgreSQL 읽기 전용 환경 점검 (Phase 1)
--
-- 실행: DBeaver 에서 대상 DB(예: seongjang_testbed)에 접속 → 이 파일 열기 → "스크립트 실행"(Alt+X)
--       결과 탭이 섹션마다 생깁니다. 각 결과를 복사(또는 CSV 저장)해서 전달해 주세요.
-- 안전: 모든 문장은 SELECT 뿐이며, 이 세션을 읽기 전용으로 바꾼 뒤 실행합니다 (설정은 이 접속에만 적용).
--       테이블 데이터 전체를 읽는 문장은 없습니다 (카탈로그·통계만 조회).
--       DBeaver 자동 커밋 모드에서 실행하세요. 섹션마다 독립 실행이라 한 섹션이 권한 오류가 나도 나머지는 계속됩니다.
--       오류가 난 섹션 번호만 알려 주시면 됩니다.
-- =====================================================================

SET default_transaction_read_only = on;
SET statement_timeout = '30s';

-- [1] 서버 버전 ----------------------------------------------------------
SELECT version() AS full_version,
       current_setting('server_version')     AS server_version,
       current_setting('server_version_num') AS server_version_num;

-- [2] 접속 정보 · SSL 적용 여부 (현재 세션) --------------------------------
SELECT current_database() AS db, current_user AS login_user, session_user,
       inet_server_addr() AS server_addr, inet_server_port() AS server_port,
       s.ssl, s.version AS ssl_version, s.cipher
FROM pg_stat_ssl s
WHERE s.pid = pg_backend_pid();

-- [3] 서버 설정 (TimescaleDB 사전 조건 · 메모리 · 백업 관련) ---------------
--     권한이 없으면 일부 행의 setting 이 보이지 않을 수 있습니다.
SELECT name, setting, unit, source
FROM pg_settings
WHERE name IN (
  'shared_preload_libraries',   -- timescaledb 가 들어 있어야 확장 사용 가능 (변경 시 재시작 필요)
  'timescaledb.license',        -- apache = 압축·연속집계·정책 일부 불가 / timescale = 전체 기능
  'timescaledb.telemetry_level',
  'max_worker_processes', 'timescaledb.max_background_workers', 'max_parallel_workers',
  'shared_buffers', 'effective_cache_size', 'work_mem', 'maintenance_work_mem',
  'max_connections', 'ssl', 'password_encryption',
  'wal_level', 'archive_mode', 'archive_command', 'max_wal_size',
  'autovacuum', 'track_commit_timestamp', 'TimeZone', 'log_timezone'
)
ORDER BY name;

-- [4] 설치된 확장 (현재 DB) -------------------------------------------------
SELECT e.extname, e.extversion, n.nspname AS schema
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
ORDER BY e.extname;

-- [5] 설치 가능한 확장 중 TimescaleDB 관련 (서버에 패키지가 깔려 있는지) ----
--     행이 없으면 서버 OS 에 TimescaleDB 패키지 설치부터 필요합니다.
SELECT name, default_version, installed_version, comment
FROM pg_available_extensions
WHERE name ILIKE '%timescale%' OR name IN ('pg_partman', 'pg_cron', 'pg_stat_statements')
ORDER BY name;

-- [6] 데이터베이스 목록 · 크기 · 소유자 -------------------------------------
SELECT d.datname, pg_get_userbyid(d.datdba) AS owner,
       pg_size_pretty(pg_database_size(d.datname)) AS size,
       d.datcollate, d.datctype, pg_encoding_to_char(d.encoding) AS encoding
FROM pg_database d
WHERE NOT d.datistemplate AND has_database_privilege(d.datname, 'CONNECT')
ORDER BY pg_database_size(d.datname) DESC;

-- [7] 현재 DB 의 스키마 목록 · 소유자 · 테이블 수 ---------------------------
SELECT n.nspname AS schema, pg_get_userbyid(n.nspowner) AS owner,
       count(c.oid) FILTER (WHERE c.relkind IN ('r', 'p')) AS tables,
       pg_size_pretty(coalesce(sum(pg_total_relation_size(c.oid)) FILTER (WHERE c.relkind IN ('r', 'p', 'm')), 0)) AS size
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname NOT LIKE 'pg_temp_%' AND n.nspname NOT LIKE 'pg_toast_temp_%'
GROUP BY n.nspname, n.nspowner
ORDER BY n.nspname;

-- [8] 기존 테이블 (용량 상위 50, 추정 행 수) --------------------------------
SELECT n.nspname AS schema, c.relname AS table_name,
       CASE c.relkind WHEN 'p' THEN 'partitioned' ELSE 'table' END AS kind,
       c.reltuples::bigint AS approx_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE '\_timescaledb%'
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 50;

-- [9] 기존 외래키 관계 -------------------------------------------------------
SELECT tc.table_schema, tc.table_name, kcu.column_name,
       ccu.table_schema AS ref_schema, ccu.table_name AS ref_table, ccu.column_name AS ref_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name AND ccu.constraint_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
ORDER BY tc.table_schema, tc.table_name;

-- [10] 이름 충돌 확인: 새로 만들 스키마·DB·테이블 이름이 이미 있는지 ---------
SELECT 'schema' AS kind, nspname AS name FROM pg_namespace WHERE nspname IN ('manufacturing', 'timeseries')
UNION ALL
SELECT 'database', datname FROM pg_database WHERE datname IN ('manufacturing')
UNION ALL
SELECT 'table', n.nspname || '.' || c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND c.relname IN ('equipment', 'equipment_node', 'aas_node_mapping', 'opcua_sample',
                    'equipment_latest', 'alarm_event');

-- [11] 현재 계정 권한 ---------------------------------------------------------
SELECT r.rolname, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
       has_database_privilege(current_database(), 'CREATE') AS can_create_schema_in_db,
       has_database_privilege(current_database(), 'TEMP')   AS can_create_temp
FROM pg_roles r
WHERE r.rolname = current_user;

-- [12] 현재 계정이 속한 롤 (pg_read_all_settings, pg_monitor 등) ---------------
SELECT b.rolname AS member_of
FROM pg_auth_members m
JOIN pg_roles b ON b.oid = m.roleid
JOIN pg_roles u ON u.oid = m.member
WHERE u.rolname = current_user
ORDER BY 1;

-- [13] 스키마별 현재 계정 권한 (public 포함) -----------------------------------
SELECT n.nspname AS schema,
       has_schema_privilege(n.oid, 'USAGE')  AS usage,
       has_schema_privilege(n.oid, 'CREATE') AS create
FROM pg_namespace n
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n.nspname NOT LIKE 'pg_temp_%' AND n.nspname NOT LIKE 'pg_toast_temp_%'
ORDER BY n.nspname;

-- [14] 테이블스페이스 · 크기 (디스크 여유 공간은 SQL 로 확인 불가 → 서버 관리자 df -h 필요) ---
SELECT spcname, pg_get_userbyid(spcowner) AS owner,
       pg_size_pretty(pg_tablespace_size(oid)) AS size
FROM pg_tablespace
ORDER BY spcname;

-- [15] 백업 관련: WAL 아카이브 상태 (권한 없으면 오류 가능) ---------------------
SELECT archived_count, last_archived_wal, last_archived_time, failed_count, last_failed_time
FROM pg_stat_archiver;

-- [16] 현재 부하 (접속 수 · 상태) — 운영 영향 판단용 ---------------------------
SELECT datname, state, count(*) AS sessions
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY datname, state
ORDER BY sessions DESC;

-- [17] DB 별 트랜잭션·쓰기량 (서버 기동 이후 누적) ------------------------------
SELECT datname, xact_commit, xact_rollback, tup_inserted, tup_updated, tup_deleted,
       stats_reset
FROM pg_stat_database
WHERE datname IS NOT NULL AND has_database_privilege(datname, 'CONNECT')
ORDER BY tup_inserted DESC;

-- [18] TimescaleDB 가 이미 설치된 경우에만: 하이퍼테이블 목록 -------------------
--      확장이 없으면 "relation does not exist" 오류가 정상입니다 (무시).
SELECT hypertable_schema, hypertable_name, num_chunks, compression_enabled
FROM timescaledb_information.hypertables;
