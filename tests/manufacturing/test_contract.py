"""운영 DB 없이 돌리는 계약 테스트.

DB 에 접속하지 않는다. 가짜 세션이 실행될 SQL 문장을 기록만 한다.
  - 코드의 테이블·컬럼이 운영 DB(schema manufacturing)에서 확인된 목록과 정확히 같은가
  - 앱 실행 경로에 DDL(테이블 생성·변경)과 시뮬레이션 코드가 없는가
  - 저장 로직이 REAL 설비의 활성 노드만, 올바른 컬럼·타입·시각·품질로 쓰는가
  - 원본 코드 동작(OPC-UA 구독 노드, 자동 접속 기본값)이 바뀌지 않았는가
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.manufacturing.db import create_engine, database_url
from app.manufacturing.ingest import NodeCache, ingest_batch
from app.manufacturing.models import Base
from app.manufacturing.recorder import OpcUaRecorder
from app.manufacturing.schemas import IngestSample
from app.opc_ua import OpcUaSession
from app.opc_ua.client import DataChange
from config import ManufacturingConfig, load_manufacturing_config

ROOT = Path(__file__).resolve().parents[2]

# 운영 DB 에서 확인된 컬럼 (사용자 제공 목록)
PRODUCTION_COLUMNS = {
    "equipment": "equipment_id equipment_type equipment_name manufacturer model_name serial_number aas_id "
                 "opcua_endpoint enabled data_source",
    "equipment_node": "equipment_id node_id node_pk node_name data_type unit sampling_ms enabled",
    "aas_node_mapping": "mapping_id equipment_id node_id aas_submodel_id aas_id_short_path mapping_type",
    "opcua_sample": "ts node_pk value_num value_bool value_text quality source_ts received_at",
    "equipment_latest": "equipment_id node_id node_pk last_ts value_num value_bool value_text quality source_ts "
                        "received_at updated_at",
    "alarm_event": "alarm_id equipment_id node_id alarm_type severity message started_at ended_at status created_at",
}

FIELD = "ROBOT_FIELD_01"
ENDPOINT = "opc.tcp://field.test:4840"


def _py_files(*parts: str) -> list[Path]:
    files: list[Path] = []
    for part in parts:
        path = ROOT / part
        files += [path] if path.is_file() else [p for p in path.rglob("*.py") if "__pycache__" not in p.parts]
    return files


# ------------------------------------------------------------------ 운영 테이블 정합성

def test_model_columns_match_production_exactly():
    tables = {t.name: set(t.columns.keys()) for t in Base.metadata.sorted_tables}
    assert set(tables) == set(PRODUCTION_COLUMNS)
    for name, columns in tables.items():
        assert columns == set(PRODUCTION_COLUMNS[name].split()), name
    assert {t.schema for t in Base.metadata.sorted_tables} == {"manufacturing"}


# ------------------------------------------------------------------ DDL·시뮬레이션 제거

DDL = re.compile(
    r"\b(create\s+(unique\s+)?(table|schema|index|extension|sequence)|alter\s+table|drop\s+(table|schema|index)"
    r"|truncate\s+table|create_hypertable|add_compression_policy|add_retention_policy)\b"
    r"|create_all\s*\(|\balembic\b",
    re.I,
)


def test_no_ddl_or_schema_initialisation_in_application_code():
    offenders = [f"{p.relative_to(ROOT)}: {m.group(0)}"
                 for p in _py_files("app", "scripts", "utils", "config.py")
                 for m in [DDL.search(p.read_text(encoding="utf-8"))] if m]
    assert offenders == []
    for leftover in ("migrations", "alembic.ini", "docker-compose.yml", "sql/schema", "sql/indexes", "sql/deploy"):
        assert not (ROOT / leftover).exists(), leftover


def test_no_simulation_code_or_routes():
    assert not (ROOT / "simulator").exists()
    assert [p.name for p in (ROOT / "scripts").glob("*") if "simulat" in p.name.lower()] == []
    pattern = re.compile(r"simulat|SIMULATION|synthetic", re.I)
    offenders = []
    for suffix in ("*.py", "*.js", "*.html", "*.css"):
        for path in (ROOT / "app").rglob(suffix):
            if "__pycache__" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []

    from app.main import app

    assert [p for p in app.openapi()["paths"] if "simulat" in p.lower()] == []


def test_write_statements_only_in_ingest_and_alarms():
    write = re.compile(r"\b(insert\s+into|update\s+manufacturing|delete\s+from)\b|\.delete\(|\bdelete\(", re.I)
    allowed = {"ingest.py", "alarms.py"}
    for path in _py_files("app/manufacturing"):
        found = bool(write.search(path.read_text(encoding="utf-8")))
        assert found == (path.name in allowed) or (found is False and path.name in allowed), path.name
    # 운영 데이터를 지우는 기능은 없다
    for path in _py_files("app/manufacturing"):
        assert not re.search(r"delete\s+from|\.delete\(", path.read_text(encoding="utf-8"), re.I), path.name


def test_password_only_used_for_connecting():
    allowed = {"config.py", "db.py", "mfg_inspect.py", "mfg_verify_readonly.py"}
    users = {p.name for p in _py_files("app", "scripts", "config.py") if "db_password" in p.read_text(encoding="utf-8")}
    assert users <= allowed, users


# ------------------------------------------------------------------ 설정·연결

ENV_KEYS = ["MFG_ENABLED", "MFG_DB_HOST", "MFG_DB_PORT", "MFG_DB_NAME", "MFG_DB_USER", "MFG_DB_PASSWORD",
            "MFG_EQUIPMENT_ID", "MFG_RECORD_OPCUA", "MFG_ALARMS_ENABLED", "OPCUA_AUTO_CONNECT"]


@pytest.fixture
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_config_targets_production_db_by_default_and_hides_secrets(clean_env):
    clean_env.setenv("MFG_ENABLED", "true")
    clean_env.setenv("MFG_DB_USER", "svc_user")
    clean_env.setenv("MFG_DB_PASSWORD", "s3cr3t-pw")
    cfg = load_manufacturing_config()
    assert (cfg.db_host, cfg.db_port, cfg.db_name, cfg.equipment_id) == ("127.0.0.1", 25432, "seongjang_testbed", FIELD)
    assert cfg.record_opcua is False and cfg.alarms_enabled is False  # 승인 전에는 수집·알람 꺼짐
    assert "s3cr3t-pw" not in repr(cfg) and "s3cr3t-pw" not in str(cfg)
    assert "s3cr3t-pw" not in repr(database_url(cfg))


def test_config_refuses_leftover_dev_db_settings_and_missing_credentials(clean_env):
    clean_env.setenv("MFG_DB_PORT", "55432")
    with pytest.raises(RuntimeError, match="개발 DB"):
        load_manufacturing_config()
    clean_env.setenv("MFG_DB_PORT", "25432")
    clean_env.setenv("MFG_DB_NAME", "manufacturing_db")
    with pytest.raises(RuntimeError, match="개발 DB"):
        load_manufacturing_config()
    clean_env.setenv("MFG_DB_NAME", "seongjang_testbed")
    clean_env.setenv("MFG_ENABLED", "true")
    clean_env.setenv("MFG_DB_USER", "svc_user")
    with pytest.raises(RuntimeError, match="MFG_DB_PASSWORD"):
        load_manufacturing_config()
    clean_env.setenv("MFG_DB_PASSWORD", "x")
    clean_env.delenv("MFG_DB_USER")
    with pytest.raises(RuntimeError, match="MFG_DB_USER"):
        load_manufacturing_config()


def test_engine_is_lazy_asyncpg_pool_and_disposable():
    cfg = ManufacturingConfig(enabled=True, db_host="127.0.0.1", db_port=25432, db_name="seongjang_testbed",
                              db_user="svc_user", db_password="s3cr3t-pw")
    engine = create_engine(cfg)  # 연결을 열지 않는다
    try:
        assert engine.url.drivername == "postgresql+asyncpg"
        assert (engine.url.host, engine.url.port, engine.url.database) == ("127.0.0.1", 25432, "seongjang_testbed")
        assert engine.pool.size() == 5 and "s3cr3t-pw" not in repr(engine.url)
    finally:
        asyncio.run(engine.dispose())


# ------------------------------------------------------------------ 원본 동작 보존

def test_original_opcua_behaviour_is_preserved(clean_env):
    assert load_manufacturing_config().auto_connect_opcua is True  # 원본: 시작할 때 OPC-UA 에 접속
    session = OpcUaSession()
    assert not hasattr(session, "node_filter")  # 구독 노드를 줄이지 않는다 (원본: 매핑된 노드 전부)
    assert session._listeners == []  # 기록기를 붙이기 전에는 수신 경로가 원본과 같다


def test_existing_and_new_routes_registered_and_disabled_state_is_503():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert {"/api/health", "/api/packages", "/api/packages/{package_id}/tree", "/api/opcua/status",
            "/api/opcua/connect", "/api/opcua/subscribe", "/api/opcua/packages/{package_id}/live"} <= paths
    assert {p.removeprefix("/api/v1/timeseries") for p in paths if p.startswith("/api/v1/")} == {
        "/alarms", "/db-status", "/equipment", "/history", "/history.csv", "/ingest", "/latest", "/mapping",
        "/mappings", "/node-status", "/nodes", "/recent", "/stream", "/summary"}
    # lifespan 을 돌리지 않으면 DB 런타임이 없다 → DB 에 접속하지 않고 503 만 돌려준다
    response = TestClient(app).get("/api/v1/timeseries/summary")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "disabled"


def test_frontend_script_only_uses_existing_element_ids():
    html = (ROOT / "app/static/viewer.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/tsdb-monitor.js").read_text(encoding="utf-8")
    used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', script))
    assert [i for i in used if f'id="{i}"' not in html] == []
    for original in ("package-list", "tree", "live-btn", "live-view-btn", "live-json-btn", "download-btn",
                     "upload-files-btn", "detail"):
        assert f'id="{original}"' in html  # 원본 AASX Viewer 요소가 그대로 있다


# ------------------------------------------------------------------ 저장 로직 (가짜 세션, DB 접속 없음)

class FakeResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def all(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class FakeSession:
    """실행될 SQL 을 기록하고, 테이블 이름으로 구분해 알맞은 가짜 결과를 돌려준다."""

    def __init__(self, nodes, latest=(), duplicates=(), fail_insert=False):
        self.nodes, self.latest, self.duplicates, self.fail_insert = nodes, list(latest), set(duplicates), fail_insert
        self.statements: list[tuple[str, object]] = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.statements.append((sql, params))
        head = " ".join(sql.split()).upper()
        if head.startswith("SELECT") and "FROM MANUFACTURING.EQUIPMENT_NODE" in head:
            return FakeResult(self.nodes)
        if head.startswith("SELECT") and "FROM MANUFACTURING.EQUIPMENT_LATEST" in head:
            return FakeResult(self.latest)
        if head.startswith("INSERT INTO MANUFACTURING.OPCUA_SAMPLE"):
            if self.fail_insert:
                raise ConnectionError("db down")
            return FakeResult((p["node_pk"], p["ts"]) for p in params if (p["node_pk"], p["ts"]) not in self.duplicates)
        if head.startswith("INSERT INTO MANUFACTURING.EQUIPMENT_LATEST"):
            return FakeResult([])
        raise AssertionError(f"예상하지 못한 SQL: {sql[:80]}")

    async def commit(self):
        self.commits += 1

    def inserts(self, table):
        return [(sql, params) for sql, params in self.statements if sql.lstrip().startswith(f"INSERT INTO manufacturing.{table}")]


def _node(node_id, pk, data_type, enabled=True, data_source="REAL", equipment=FIELD):
    return SimpleNamespace(equipment_id=equipment, node_id=node_id, node_pk=pk, data_type=data_type,
                           enabled=enabled, data_source=data_source)


NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
NODES = [
    _node("ns=2;i=1145", 1, "Double"),
    _node("ns=2;i=1166", 2, "Double", enabled=False),   # 현장 미지원(비활성) 노드
    _node("ns=2;i=1170", 3, "Boolean"),
    _node("ns=2;i=1171", 4, "String"),
    _node("ns=2;i=1172", 5, "Int32"),
]


def _sample(node_id, value, *, source_ts=NOW - timedelta(seconds=1), received_at=NOW - timedelta(milliseconds=900),
            quality=0, equipment=FIELD):
    return IngestSample(equipment_id=equipment, node_id=node_id, value=value, quality=quality,
                        source_ts=source_ts, received_at=received_at)


def _ingest(session, samples, data_source="REAL"):
    return asyncio.run(ingest_batch(session, NodeCache(), samples, data_source=data_source, now=NOW))


def test_ingest_stores_only_enabled_real_nodes_with_typed_columns():
    session = FakeSession(NODES)
    result = _ingest(session, [
        _sample("ns=2;i=1145", 12.5),
        _sample("ns=2;i=1166", 1.0),            # 비활성 → 저장하지 않음
        _sample("ns=2;i=1170", True),
        _sample("ns=2;i=1171", "AUTO"),
        _sample("ns=2;i=1172", 7),
        _sample("ns=2;i=9999", 1.0),            # 등록되지 않은 노드
    ])
    assert (result.inserted, result.rejected) == (4, 2)
    assert [r.reason for r in result.rejected_samples] == ["node_disabled", "unknown_node"]
    (_, params), = session.inserts("opcua_sample")
    by_pk = {p["node_pk"]: p for p in params}
    assert set(by_pk) == {1, 3, 4, 5}  # node_id → node_pk 로 정확히 변환, 비활성 node_pk=2 는 없음
    assert (by_pk[1]["value_num"], by_pk[1]["value_bool"], by_pk[1]["value_text"]) == (12.5, None, None)
    assert (by_pk[3]["value_num"], by_pk[3]["value_bool"], by_pk[3]["value_text"]) == (None, True, None)
    assert (by_pk[4]["value_num"], by_pk[4]["value_bool"], by_pk[4]["value_text"]) == (None, None, "AUTO")
    assert (by_pk[5]["value_num"], by_pk[5]["value_bool"], by_pk[5]["value_text"]) == (7.0, None, None)
    assert session.commits == 1


def test_ingest_separates_ts_source_ts_received_at_and_keeps_quality():
    session = FakeSession(NODES)
    source, received = NOW - timedelta(seconds=2), NOW - timedelta(seconds=1)
    only_received = NOW - timedelta(milliseconds=500)
    _ingest(session, [
        _sample("ns=2;i=1145", 1.0, source_ts=source, received_at=received),
        _sample("ns=2;i=1170", None, source_ts=None, received_at=only_received, quality=0x80310000),
    ])
    (_, params), = session.inserts("opcua_sample")
    by_pk = {p["node_pk"]: p for p in params}
    assert (by_pk[1]["ts"], by_pk[1]["source_ts"], by_pk[1]["received_at"]) == (source, source, received)
    assert (by_pk[3]["ts"], by_pk[3]["source_ts"], by_pk[3]["received_at"]) == (only_received, None, only_received)
    assert by_pk[3]["quality"] == 0x80310000 and by_pk[3]["value_bool"] is None  # Bad: 값 NULL, StatusCode 보존


def test_ingest_rejects_non_real_source_future_and_naive_timestamps():
    session = FakeSession(NODES)
    mismatch = _ingest(session, [_sample("ns=2;i=1145", 1.0)], data_source="SIMULATION")
    assert (mismatch.inserted, [r.reason for r in mismatch.rejected_samples]) == (0, ["data_source_mismatch"])
    assert session.inserts("opcua_sample") == []
    other = FakeSession([_node("ns=2;i=1145", 1, "Double", data_source="SIMULATION")])
    assert _ingest(other, [_sample("ns=2;i=1145", 1.0)]).rejected_samples[0].reason == "data_source_mismatch"
    reasons = [r.reason for r in _ingest(FakeSession(NODES), [
        _sample("ns=2;i=1145", 1.0, source_ts=NOW + timedelta(hours=1)),
        _sample("ns=2;i=1145", 1.0, source_ts=datetime(2026, 11, 2, 11, 59)),
    ]).rejected_samples]
    assert reasons == ["future_timestamp", "timezone_required"]


def test_ingest_duplicates_and_latest_never_overwritten_by_older_data():
    ts = NOW - timedelta(seconds=3)
    # 같은 배치 안 중복은 하나만, 이미 저장된 (node_pk, ts)는 INSERT 가 돌려주지 않아 중복으로 센다
    session = FakeSession(NODES, duplicates={(3, ts)})
    result = _ingest(session, [
        _sample("ns=2;i=1145", 1.0, source_ts=ts), _sample("ns=2;i=1145", 9.9, source_ts=ts),
        _sample("ns=2;i=1170", True, source_ts=ts),
    ])
    assert (result.inserted, result.duplicates) == (1, 2)
    sql = session.inserts("opcua_sample")[0][0]
    assert "ON CONFLICT (node_pk, ts) DO NOTHING" in sql
    latest_sql = session.inserts("equipment_latest")[0][0]
    assert "ON CONFLICT (equipment_id, node_id) DO UPDATE" in latest_sql
    assert "WHERE excluded.last_ts > manufacturing.equipment_latest.last_ts" in latest_sql  # 과거 값은 덮어쓰지 않음
    # 새로 저장된 행이 하나도 없으면 최신값 UPSERT 자체를 하지 않는다
    all_dup = FakeSession(NODES, duplicates={(1, ts)})
    assert _ingest(all_dup, [_sample("ns=2;i=1145", 1.0, source_ts=ts)]).duplicates == 1
    assert all_dup.inserts("equipment_latest") == []
    # 이미 더 최신 값이 있으면 '늦게 도착'으로 센다 (저장은 하되 최신값 갱신은 DB 조건이 막는다)
    late = _ingest(FakeSession(NODES, latest=[(1, NOW)]), [_sample("ns=2;i=1145", 1.0, source_ts=ts)])
    assert late.late == 1


# ------------------------------------------------------------------ 수집기 (가짜 세션)

def _recorder(session):
    @asynccontextmanager
    async def sessions():
        yield session

    runtime = SimpleNamespace(cfg=SimpleNamespace(equipment_id=FIELD), sessions=sessions, nodes=NodeCache())
    return OpcUaRecorder(runtime, lambda: ENDPOINT)


def test_recorder_selects_only_enabled_real_nodes_of_the_field_equipment_and_endpoint():
    index_rows = [SimpleNamespace(node_id=n.node_id, equipment_id=FIELD) for n in NODES if n.enabled]

    class IndexSession(FakeSession):
        async def execute(self, stmt, params=None):
            self.statements.append((str(stmt.compile(dialect=postgresql.dialect(),
                                                     compile_kwargs={"literal_binds": True})), params))
            return FakeResult(index_rows)

    session = IndexSession(NODES)
    recorder = _recorder(session)
    asyncio.run(recorder._refresh_index())
    sql = session.statements[0][0]
    for required in ("manufacturing.equipment.data_source = 'REAL'", "manufacturing.equipment.enabled",
                     "manufacturing.equipment_node.enabled", f"manufacturing.equipment.opcua_endpoint = '{ENDPOINT}'",
                     f"manufacturing.equipment.equipment_id = '{FIELD}'"):
        assert required in sql, required

    def change(node_id, value, status_code=0, naive=True):
        ts = datetime(2026, 11, 2, 12, 0, 0) if naive else NOW
        return DataChange(node_id, value, "Good", status_code == 0, ts, ts, status_code)

    received = NOW
    recorder.on_change(change("ns=2;i=1145", 3.5), received)
    recorder.on_change(change("ns=2;i=1166", 1.0), received)   # 비활성(현장 미지원) → 저장 대상 아님
    recorder.on_change(change("ns=2;i=1170", None, 0x80000000), received)
    assert (recorder.received, recorder.unmapped, len(recorder._buffer)) == (3, 1, 2)
    sample = recorder._buffer[0]
    assert sample.source_ts == NOW and sample.source_ts.tzinfo is not None  # asyncua 의 naive UTC → aware UTC
    assert recorder._buffer[1].quality == 0x80000000 and recorder._buffer[1].value is None


def test_recorder_keeps_buffer_when_db_is_down_and_drains_after_recovery():
    session = FakeSession(NODES, fail_insert=True)
    recorder = _recorder(session)
    recorder._index = {"ns=2;i=1145": FIELD}
    recorder.indexed_endpoint = ENDPOINT
    # 저장 로직은 실제 현재 시각 기준으로 미래 시각을 거부하므로, 여기서는 실제 현재 시각에서 10초 전을 쓴다.
    ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).replace(tzinfo=None)  # asyncua 처럼 naive UTC
    for k in range(3):
        recorder.on_change(DataChange("ns=2;i=1145", float(k), "Good", True, ts + timedelta(milliseconds=200 * k),
                                      ts, 0), datetime.now(timezone.utc))
    with pytest.raises(ConnectionError):
        asyncio.run(recorder._flush())
    assert len(recorder._buffer) == 3 and recorder.dropped == 0  # 유실 없이 되돌려 놓음
    session.fail_insert = False
    asyncio.run(recorder._flush())
    assert len(recorder._buffer) == 0 and recorder.inserted == 3
    # 종료 시 DB 가 내려가 있어도 예외 없이 끝난다
    down = _recorder(FakeSession(NODES, fail_insert=True))
    down._index, down.indexed_endpoint = {"ns=2;i=1145": FIELD}, ENDPOINT
    down.on_change(DataChange("ns=2;i=1145", 1.0, "Good", True, ts, ts, 0), datetime.now(timezone.utc))
    asyncio.run(down.stop())


# ------------------------------------------------------------------ 운영 서버 점검 스크립트 판정

def test_preflight_passes_with_expected_production_state_and_flags_each_gap():
    from app.opcua_catalog import load_catalog
    from scripts.mfg_inspect import preflight

    endpoint = load_catalog().default_server().endpoint
    base_indexes = [
        {"tablename": "opcua_sample", "indexdef": "CREATE UNIQUE INDEX opcua_sample_pkey ON manufacturing.opcua_sample USING btree (node_pk, ts)"},
        {"tablename": "equipment_latest", "indexdef": "CREATE UNIQUE INDEX equipment_latest_pkey ON manufacturing.equipment_latest USING btree (equipment_id, node_id)"},
        {"tablename": "equipment_node", "indexdef": "CREATE UNIQUE INDEX equipment_node_pkey ON manufacturing.equipment_node USING btree (equipment_id, node_id)"},
        {"tablename": "equipment_node", "indexdef": "CREATE UNIQUE INDEX equipment_node_node_pk_key ON manufacturing.equipment_node USING btree (node_pk)"},
        {"tablename": "alarm_event", "indexdef": "CREATE UNIQUE INDEX alarm_event_open_uniq ON manufacturing.alarm_event USING btree (equipment_id, COALESCE(node_id, ''::text), alarm_type) WHERE (status <> 'CLEARED'::text)"},
    ]
    columns = [{"table_name": t.name, "column_name": c} for t in Base.metadata.sorted_tables for c in t.columns.keys()]
    equipment = {"data_source": "REAL", "enabled": True, "opcua_endpoint": endpoint}

    def result(indexes=base_indexes, cols=columns, field=None):
        return {"indexes": indexes, "columns": cols, "hypertable": [{"column_name": "ts"}],
                "field": field or {"total": 23, "enabled": 18, "mappings": 23, "unmapped": 0}}

    ok = preflight(result(), equipment)
    assert ok["all_pass"] is True, ok["checks"]
    no_partial = preflight(result(indexes=base_indexes[:4]), equipment)
    assert no_partial["checks"]["alarm_event_open_unique_partial_index"] is False and not no_partial["all_pass"]
    wrong_endpoint = preflight(result(), {**equipment, "opcua_endpoint": "opc.tcp://other:4840"})
    assert wrong_endpoint["checks"]["field_endpoint_matches_catalog"] is False
    missing_col = preflight(result(cols=[c for c in columns if c["column_name"] != "sampling_ms"]), equipment)
    assert missing_col["checks"]["model_columns_all_exist_in_db"] is False
    nodes = preflight(result(field={"total": 23, "enabled": 17, "mappings": 23, "unmapped": 0}), equipment)
    assert nodes["checks"]["field_nodes_23_active_18_inactive_5_unmapped_0"] is False
    assert preflight(result(), None)["checks"]["field_equipment_exists"] is False


# ------------------------------------------------------------------ 오류 분류 · 노드 상태

def test_db_unavailable_classification_uses_sqlstate():
    from sqlalchemy.exc import DBAPIError

    from app.manufacturing.api import is_db_unavailable

    def dbapi(sqlstate):
        orig = Exception("x")
        orig.sqlstate = sqlstate
        return DBAPIError("SELECT 1", None, orig)

    assert is_db_unavailable(ConnectionRefusedError())
    for state in ("28P01", "3D000", "08006", "57P03", "53300"):   # 비밀번호 오류, DB 없음, 연결, 서버 기동 중, 연결 수 초과
        assert is_db_unavailable(dbapi(state)), state
    for state in ("42703", "42P01", "23505", None):                # 컬럼·테이블 없음, 제약 위반은 코드/데이터 문제 → 503 으로 숨기지 않는다
        assert not is_db_unavailable(dbapi(state)), state
    assert not is_db_unavailable(ValueError("x"))


def test_node_status_shows_disabled_nodes_and_marks_failed_subscriptions_unsupported():
    from app.manufacturing import monitor

    now = datetime.now(timezone.utc)

    def row(node_id, pk, enabled, received=None):
        return SimpleNamespace(
            equipment_id=FIELD, node_id=node_id, node_pk=pk, node_name=f"n{pk}", data_type="Double", unit=None,
            sampling_ms=100, enabled=enabled, equipment_type="ROBOT", data_source="REAL", last_ts=received,
            value_num=1.0 if received else None, value_bool=None, value_text=None,
            quality=0 if received else None, received_at=received, aas_submodel_id="sm", aas_id_short_path=f"p{pk}")

    rows = [row("a", 1, True, now), row("b", 2, True), row("c", 3, False), row("d", 4, True)]

    class Session:
        def __init__(self):
            self.calls = []

        async def execute(self, stmt, params=None):
            self.calls.append(params)
            return FakeResult(rows if len(self.calls) == 1 else [])

        async def get(self, model, key):
            return SimpleNamespace(data_source="REAL", opcua_endpoint=ENDPOINT)

    session = Session()
    out = asyncio.run(monitor.node_status(
        session, FIELD, subscription={"failedNodes": {"d": "BadNodeIdUnknown"}}, subscribed_endpoint=ENDPOINT))
    assert {r["node_id"]: r["state"] for r in out} == {
        "a": "RECEIVING", "b": "NO_DATA", "c": "DISABLED", "d": "UNSUPPORTED"}
    assert session.calls[0]["all"] is True  # 비활성 노드까지 조회해서 화면에서 사라지지 않게 한다
    assert {r["node_id"]: r["state_label"] for r in out}["c"] == "미수집 (비활성)"
