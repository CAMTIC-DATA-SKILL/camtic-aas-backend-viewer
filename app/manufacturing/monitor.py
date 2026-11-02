"""모니터링 화면용 쿼리: 요약, 실시간 스트림, DB 상태, AAS Property ↔ 노드 조회.

대용량 테이블에서 화면 갱신마다 전체 COUNT(*) 를 하지 않는다.
- 최근 건수: 활성 노드별 PK(node_pk, ts) 인덱스 범위 (최근 60초)
- 전체 행 수: TimescaleDB approximate_row_count (통계 기반 근사)
- 최신값: equipment_latest (노드 수만큼의 작은 테이블)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import ARRAY, Integer, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Equipment
from .queries import STALE_FACTOR, value_of
from .values import quality_class

WINDOW_S = 60  # SQL 의 make_interval(secs => 60) 과 같아야 한다
STREAM_LOOKBACK = timedelta(minutes=10)


async def summary(session: AsyncSession) -> dict[str, Any]:
    eq = (await session.execute(text("""
        SELECT equipment_type, data_source, count(*) AS n
        FROM manufacturing.equipment WHERE enabled GROUP BY 1, 2
    """))).all()
    nodes = (await session.execute(text("""
        SELECT n.node_pk, n.sampling_ms, l.received_at, l.quality
        FROM manufacturing.equipment_node n
        JOIN manufacturing.equipment e USING (equipment_id)
        LEFT JOIN manufacturing.equipment_latest l ON l.node_pk = n.node_pk
        WHERE n.enabled AND e.enabled
    """))).all()
    now = datetime.now(timezone.utc)
    pks = [r.node_pk for r in nodes]
    recent = 0
    if pks:
        recent = (await session.execute(text("""
            SELECT count(*) FROM manufacturing.opcua_sample
            WHERE node_pk = ANY(:pks) AND ts >= now() - make_interval(secs => 60)
        """).bindparams(bindparam("pks", type_=ARRAY(Integer))), {"pks": pks})).scalar_one()
    fresh = [r for r in nodes if r.received_at and now - r.received_at <= timedelta(milliseconds=r.sampling_ms * STALE_FACTOR)]
    by_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in eq:
        by_type[row.equipment_type] = by_type.get(row.equipment_type, 0) + row.n
        by_source[row.data_source] = by_source.get(row.data_source, 0) + row.n
    return {
        "equipment_total": sum(by_type.values()),
        "equipment_by_type": {t: by_type.get(t, 0) for t in ("ROBOT", "PLC", "AMR", "SENSOR", "MACHINE")},
        "equipment_by_source": by_source,
        "nodes_total": len(nodes),
        "nodes_receiving": len(fresh),
        "nodes_receiving_good": sum(1 for r in fresh if quality_class(r.quality) == "GOOD"),
        "samples_last_minute": recent,
        "actual_rows_per_s": round(recent / WINDOW_S, 1),
        "target_rows_per_s_all_nodes": round(sum(1000 / r.sampling_ms for r in nodes), 1),
        "target_rows_per_s_receiving": round(sum(1000 / r.sampling_ms for r in fresh), 1),
        "db_connected": True,
        "generated_at": now,
    }


async def equipment_overview(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT e.equipment_id, e.equipment_type, e.equipment_name, e.manufacturer, e.model_name, e.aas_id,
               e.opcua_endpoint, e.data_source, e.enabled,
               count(n.node_pk) FILTER (WHERE n.enabled) AS nodes,
               max(l.received_at) AS last_received_at
        FROM manufacturing.equipment e
        LEFT JOIN manufacturing.equipment_node n ON n.equipment_id = e.equipment_id
        LEFT JOIN manufacturing.equipment_latest l ON l.node_pk = n.node_pk AND n.enabled
        GROUP BY e.equipment_id ORDER BY e.equipment_id
    """))).mappings().all()
    return [dict(r) for r in rows]


async def nodes(
    session: AsyncSession,
    equipment_id: Optional[str],
    data_source: Optional[str] = None,
    include_disabled: bool = False,
) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT n.equipment_id, n.node_id, n.node_pk, n.node_name, n.data_type, n.unit, n.sampling_ms, n.enabled,
               e.equipment_type, e.data_source,
               l.last_ts, l.value_num, l.value_bool, l.value_text, l.quality, l.received_at,
               m.aas_submodel_id, m.aas_id_short_path
        FROM manufacturing.equipment_node n
        JOIN manufacturing.equipment e USING (equipment_id)
        LEFT JOIN manufacturing.equipment_latest l ON l.node_pk = n.node_pk
        LEFT JOIN LATERAL (
            SELECT aas_submodel_id, aas_id_short_path FROM manufacturing.aas_node_mapping m
            WHERE m.equipment_id = n.equipment_id AND m.node_id = n.node_id
            ORDER BY mapping_id LIMIT 1
        ) m ON true
        WHERE (n.enabled OR CAST(:all AS boolean)) AND (CAST(:eq AS text) IS NULL OR n.equipment_id = :eq)
          AND (CAST(:src AS text) IS NULL OR e.data_source = :src)
        ORDER BY n.equipment_id, n.node_pk
    """), {"eq": equipment_id, "src": data_source, "all": include_disabled})).all()
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        stale = r.received_at is None or now - r.received_at > timedelta(milliseconds=r.sampling_ms * STALE_FACTOR)
        out.append({
            "equipment_id": r.equipment_id, "equipment_type": r.equipment_type, "data_source": r.data_source,
            "node_id": r.node_id, "node_pk": r.node_pk, "node_name": r.node_name, "data_type": r.data_type,
            "unit": r.unit, "sampling_ms": r.sampling_ms, "enabled": r.enabled,
            "last_ts": r.last_ts, "received_at": r.received_at,
            "value": value_of(r.value_num, r.value_bool, r.value_text, r.data_type) if r.last_ts else None,
            "quality": r.quality,
            "quality_class": quality_class(r.quality) if r.quality is not None else "NO_DATA",
            "receiving": not stale,
            "aas_submodel_id": r.aas_submodel_id, "aas_path": r.aas_id_short_path,
        })
    return out


async def recent_rows(
    session: AsyncSession,
    *,
    equipment_id: Optional[str],
    node_id: Optional[str],
    after: Optional[datetime] = None,
    after_by_node: Optional[dict[int, datetime]] = None,
    data_source: Optional[str] = None,
    limit: int,
) -> list[dict[str, Any]]:
    """활성 노드마다 PK 인덱스로 최신 행만 읽고(LATERAL), 합쳐서 최신순 limit 개.

    after_by_node: 노드별로 이미 보낸 마지막 ts. 노드마다 따로 두어야 한다 — 현장 장비 시계가 이 PC 보다
    빠르면(ts 가 미래) 하나의 기준 시각이 앞으로 밀려 다른 노드의 새 행이 빠지기 때문이다.
    """
    floor = datetime.now(timezone.utc) - STREAM_LOOKBACK
    default_after = max(after, floor) if after else floor
    cursors = after_by_node or {}
    # 설비마다 최신 limit 행 (데이터가 많은 설비가 적은 설비 행을 밀어내지 않게)
    rows = (await session.execute(text("""
        SELECT * FROM (
        SELECT n.node_pk, n.equipment_id, e.equipment_type, e.data_source, n.node_id, n.node_name, n.unit,
               n.data_type, s.ts, s.value_num, s.value_bool, s.value_text, s.quality, s.source_ts, s.received_at,
               row_number() OVER (PARTITION BY n.equipment_id ORDER BY s.received_at DESC, n.node_pk DESC) AS rn
        FROM manufacturing.equipment_node n
        JOIN manufacturing.equipment e USING (equipment_id)
        LEFT JOIN unnest(CAST(:cpks AS int[]), CAST(:cts AS timestamptz[])) AS c(node_pk, after_ts)
               ON c.node_pk = n.node_pk
        CROSS JOIN LATERAL (
            SELECT ts, value_num, value_bool, value_text, quality, source_ts, received_at
            FROM manufacturing.opcua_sample s
            WHERE s.node_pk = n.node_pk AND s.ts > greatest(coalesce(c.after_ts, :after), :floor)
            ORDER BY s.ts DESC LIMIT :limit
        ) s
        WHERE n.enabled AND e.enabled
          AND (CAST(:eq AS text) IS NULL OR n.equipment_id = :eq)
          AND (CAST(:node AS text) IS NULL OR n.node_id = :node)
          AND (CAST(:src AS text) IS NULL OR e.data_source = :src)
        ) ranked
        WHERE rn <= :limit
        ORDER BY received_at DESC, node_pk DESC
    """), {"after": default_after, "floor": floor, "limit": limit, "eq": equipment_id, "node": node_id,
           "src": data_source, "cpks": list(cursors), "cts": list(cursors.values())})).all()
    return [{
        "node_pk": r.node_pk,
        "ts": r.ts, "received_at": r.received_at, "source_ts": r.source_ts,
        "equipment_id": r.equipment_id, "equipment_type": r.equipment_type, "data_source": r.data_source,
        "node_id": r.node_id, "node_name": r.node_name, "unit": r.unit,
        "value": value_of(r.value_num, r.value_bool, r.value_text, r.data_type),
        "quality": r.quality, "quality_class": quality_class(r.quality),
    } for r in rows]


async def db_status(session: AsyncSession) -> dict[str, Any]:
    info = (await session.execute(text("""
        SELECT current_setting('server_version') AS pg,
               (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS tsdb,
               current_setting('timescaledb.license', true) AS license,
               current_database() AS db
    """))).one()
    hyper = (await session.execute(text("""
        SELECT h.num_chunks, h.compression_enabled,
               (SELECT count(*) FROM timescaledb_information.chunks c
                 WHERE c.hypertable_schema = 'manufacturing' AND c.hypertable_name = 'opcua_sample'
                   AND c.is_compressed) AS compressed_chunks,
               (SELECT d.time_interval FROM timescaledb_information.dimensions d
                 WHERE d.hypertable_schema = 'manufacturing' AND d.hypertable_name = 'opcua_sample') AS chunk_interval,
               hypertable_size('manufacturing.opcua_sample') AS bytes,
               approximate_row_count('manufacturing.opcua_sample') AS approx_rows,
               pg_database_size(current_database()) AS db_bytes
        FROM timescaledb_information.hypertables h
        WHERE h.hypertable_schema = 'manufacturing' AND h.hypertable_name = 'opcua_sample'
    """))).one_or_none()
    # 수신 지연(received_at - source_ts). 음수면 현장 장비 시계가 이 서버보다 빠르다는 뜻이다.
    by_source = (await session.execute(text("""
        SELECT e.data_source, count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM s.received_at - s.source_ts)) AS delay_s
        FROM manufacturing.equipment_node n
        JOIN manufacturing.equipment e USING (equipment_id)
        CROSS JOIN LATERAL (
            SELECT received_at, source_ts FROM manufacturing.opcua_sample s
            WHERE s.node_pk = n.node_pk AND s.ts >= now() - make_interval(secs => 60)
        ) s
        WHERE n.enabled
        GROUP BY e.data_source
    """))).all()
    total_recent = sum(r.n for r in by_source)
    delays = {r.data_source: round(r.delay_s * 1000, 1) for r in by_source if r.delay_s is not None}
    latest = (await session.execute(text("""
        SELECT max(l.received_at) AS last_write, max(l.updated_at) AS last_update,
               count(*) FILTER (WHERE l.quality < 1073741824) AS good,
               count(*) FILTER (WHERE l.quality >= 1073741824 AND l.quality < 2147483648) AS uncertain,
               count(*) FILTER (WHERE l.quality >= 2147483648) AS bad
        FROM manufacturing.equipment_latest l
        JOIN manufacturing.equipment_node n USING (node_pk) WHERE n.enabled
    """))).one()
    jobs = (await session.execute(text("""
        SELECT proc_name, schedule_interval::text AS every, next_start
        FROM timescaledb_information.jobs
        WHERE hypertable_schema = 'manufacturing' AND hypertable_name = 'opcua_sample'
    """))).mappings().all()
    return {
        "db_connected": True,
        "database": info.db,
        "postgresql_version": info.pg,
        "timescaledb_version": info.tsdb,
        "timescaledb_license": info.license,
        "hypertable": None if hyper is None else {
            "name": "manufacturing.opcua_sample",
            "chunks": hyper.num_chunks,
            "compressed_chunks": hyper.compressed_chunks,
            "chunk_interval": str(hyper.chunk_interval) if hyper.chunk_interval else None,
            "compression_enabled": hyper.compression_enabled,
            "size_bytes": hyper.bytes,
            "approx_rows": hyper.approx_rows,
            "database_bytes": hyper.db_bytes,
        },
        "policies": [dict(j) for j in jobs],
        "last_write_at": latest.last_write,
        "samples_last_minute": total_recent,
        "rows_per_s": round(total_recent / WINDOW_S, 1),
        "samples_last_minute_by_source": {r.data_source: r.n for r in by_source},
        # received_at - source_ts 중앙값. 음수면 장비(서버) 시계가 이 PC 보다 빠르다는 뜻이다.
        "receive_delay_median_ms_by_source": delays,
        "since_last_write_s": round((datetime.now(timezone.utc) - latest.last_write).total_seconds(), 1)
        if latest.last_write else None,
        "latest_quality": {"good": latest.good, "uncertain": latest.uncertain, "bad": latest.bad},
    }


async def mapping_lookup(session: AsyncSession, submodel_id: str, path: str) -> list[dict[str, Any]]:
    """AAS 화면에서 고른 Property(Submodel id + idShort 경로)에 연결된 노드(비활성 포함). 매핑이 없으면 빈 목록."""
    rows = (await session.execute(text("""
        SELECT m.equipment_id, e.equipment_name, e.equipment_type, e.data_source, e.aas_id,
               n.node_id, n.node_pk, n.node_name, n.unit, n.data_type, n.enabled, l.received_at
        FROM manufacturing.aas_node_mapping m
        JOIN manufacturing.equipment e ON e.equipment_id = m.equipment_id
        JOIN manufacturing.equipment_node n ON n.equipment_id = m.equipment_id AND n.node_id = m.node_id
        LEFT JOIN manufacturing.equipment_latest l ON l.node_pk = n.node_pk
        WHERE m.aas_submodel_id = :sm AND m.aas_id_short_path = :path AND e.enabled
        -- 활성 노드 → 지금 수신 중인 설비 → 그중 실제(REAL) 설비 → 마지막 수신이 최근인 설비 순 (비활성 노드도 매핑은 보여 준다)
        ORDER BY n.enabled DESC, (l.received_at > now() - interval '5 seconds') DESC NULLS LAST,
                 (e.data_source = 'REAL') DESC, l.received_at DESC NULLS LAST, m.equipment_id
    """), {"sm": submodel_id, "path": path})).mappings().all()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 노드 상태 (현장 미지원·미수집 표시)

NODE_STATES = {
    "RECEIVING": "수신 중",
    "STALE": "미수집 (수신 끊김)",
    "UNSUPPORTED": "현장 미지원",
    "DISABLED": "미수집 (비활성)",
    "NO_DATA": "데이터 없음",
}


async def node_status(
    session: AsyncSession, equipment_id: str, *, subscription: Optional[dict[str, Any]] = None,
    subscribed_endpoint: Optional[str] = None,
) -> list[dict[str, Any]]:
    """노드별 상태와 최근 60초 실제 수집 간격(ts 간격 중앙값). 비활성 노드도 지우지 않고 상태로만 구분한다."""
    rows = await nodes(session, equipment_id, include_disabled=True)
    intervals = {r.node_pk: r for r in (await session.execute(text("""
        SELECT b.node_pk, count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY gap) AS p95_ms
        FROM unnest(CAST(:pks AS int[])) AS b(node_pk)
        CROSS JOIN LATERAL (
            SELECT extract(epoch FROM ts - lag(ts) OVER (ORDER BY ts)) * 1000 AS gap
            FROM manufacturing.opcua_sample s
            WHERE s.node_pk = b.node_pk AND s.ts >= now() - make_interval(secs => 60)
        ) g
        WHERE gap IS NOT NULL
        GROUP BY b.node_pk
    """), {"pks": [r["node_pk"] for r in rows]})).all()}
    equipment = await session.get(Equipment, equipment_id)
    failed: dict[str, str] = {}
    revised: dict[str, dict] = {}
    if (equipment is not None and equipment.data_source == "REAL" and subscription
            and equipment.opcua_endpoint and equipment.opcua_endpoint == subscribed_endpoint):
        failed = subscription.get("failedNodes") or {}
        revised = subscription.get("revisedNodes") or {}
    out = []
    for r in rows:
        if r["node_id"] in failed:
            state = "UNSUPPORTED"
        elif not r["enabled"]:
            state = "DISABLED"
        elif r["receiving"]:
            state = "RECEIVING"
        elif r["last_ts"] is not None:
            state = "STALE"
        else:
            state = "NO_DATA"
        gap = intervals.get(r["node_pk"])
        out.append({
            **r,
            "state": state,
            "state_label": NODE_STATES[state] + (f" ({failed[r['node_id']]})" if state == "UNSUPPORTED" else ""),
            "interval_median_ms": round(gap.median_ms, 1) if gap else None,
            "interval_p95_ms": round(gap.p95_ms, 1) if gap else None,
            "samples_60s": gap.n + 1 if gap else (0 if r["last_ts"] is None else None),
            "revised_sampling_ms": revised.get(r["node_id"], {}).get("samplingMs"),
            "revised_queue_size": revised.get(r["node_id"], {}).get("queueSize"),
        })
    return out


# ------------------------------------------------------------------ 저장 공간 · 압축 · 증가량 (60초 캐시)

_storage_cache: dict[str, Any] = {"at": None, "value": None}
STORAGE_CACHE = timedelta(seconds=60)


async def storage(session: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if not force and _storage_cache["at"] and now - _storage_cache["at"] < STORAGE_CACHE:
        return _storage_cache["value"]
    started = datetime.now(timezone.utc)
    size = (await session.execute(text(
        "SELECT * FROM hypertable_detailed_size('manufacturing.opcua_sample')"))).mappings().one()
    comp = (await session.execute(text(
        "SELECT * FROM hypertable_compression_stats('manufacturing.opcua_sample')"))).mappings().one_or_none()
    chunks = (await session.execute(text("""
        SELECT c.chunk_name, c.range_start, c.range_end, c.is_compressed, d.total_bytes, d.index_bytes
        FROM timescaledb_information.chunks c
        JOIN chunks_detailed_size('manufacturing.opcua_sample') d ON d.chunk_name = c.chunk_name
        WHERE c.hypertable_schema = 'manufacturing' AND c.hypertable_name = 'opcua_sample'
        ORDER BY c.range_start
    """))).mappings().all()
    growth = (await session.execute(text("""
        SELECT e.data_source,
               count(*) FILTER (WHERE s.ts >= now() - interval '1 hour') AS rows_1h,
               count(*) AS rows_24h
        FROM manufacturing.equipment_node n
        JOIN manufacturing.equipment e USING (equipment_id)
        CROSS JOIN LATERAL (
            SELECT ts FROM manufacturing.opcua_sample s
            WHERE s.node_pk = n.node_pk AND s.ts >= now() - interval '24 hours'
        ) s
        GROUP BY e.data_source
    """))).all()
    approx = (await session.execute(text(
        "SELECT approximate_row_count('manufacturing.opcua_sample')"))).scalar_one()
    uncompressed = [c for c in chunks if not c["is_compressed"]]
    unc_bytes = sum(c["total_bytes"] or 0 for c in uncompressed)
    unc_rows = (await session.execute(text("""
        SELECT coalesce(sum(cl.reltuples), 0) FROM pg_class cl
        JOIN pg_namespace ns ON ns.oid = cl.relnamespace
        WHERE ns.nspname = '_timescaledb_internal' AND cl.relname = ANY(:names)
    """), {"names": [c["chunk_name"] for c in uncompressed]})).scalar_one()
    bytes_per_row = unc_bytes / unc_rows if unc_rows else None
    rows_1h = sum(r.rows_1h for r in growth)
    rows_24h = sum(r.rows_24h for r in growth)
    value = {
        "table_bytes": size["table_bytes"], "index_bytes": size["index_bytes"], "toast_bytes": size["toast_bytes"],
        "total_bytes": size["total_bytes"], "approx_rows": approx,
        "compression": None if comp is None else {
            "chunks": comp["total_chunks"], "compressed_chunks": comp["number_compressed_chunks"],
            "before_bytes": comp["before_compression_total_bytes"], "after_bytes": comp["after_compression_total_bytes"],
            "ratio": round(comp["before_compression_total_bytes"] / comp["after_compression_total_bytes"], 1)
            if comp["after_compression_total_bytes"] else None,
        },
        "chunks": [dict(c) for c in chunks],
        "rows_last_1h": rows_1h, "rows_last_24h": rows_24h,
        "rows_by_source": {r.data_source: {"1h": r.rows_1h, "24h": r.rows_24h} for r in growth},
        "uncompressed_bytes_per_row": round(bytes_per_row, 1) if bytes_per_row else None,
        "growth_bytes_1h_estimate": round(rows_1h * bytes_per_row) if bytes_per_row else None,
        "growth_bytes_per_day_estimate": round(rows_1h * 24 * bytes_per_row) if bytes_per_row else None,
        "measured_at": now, "query_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
    }
    _storage_cache.update(at=now, value=value)
    return value
