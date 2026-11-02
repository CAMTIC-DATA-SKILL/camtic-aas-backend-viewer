"""조회 쿼리. 모든 조회는 기간·건수 상한이 있어 전체 데이터를 무제한으로 읽지 않는다."""

from __future__ import annotations

import base64
import binascii
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import ARRAY, Double, Integer, and_, bindparam, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AasNodeMapping, Equipment, EquipmentNode, OpcuaSample
from .values import INTEGER_TYPES

MAX_RAW_RANGE = timedelta(hours=24)
MAX_BUCKET_RANGE = timedelta(days=31)
MAX_RAW_LIMIT = 10_000
MAX_POINTS = 5_000
MAX_NODES = 20
MIN_BUCKET_SECONDS = 0.1
STALE_FACTOR = 50  # sampling_ms × 50 (100ms 노드는 5초) 동안 수신이 없으면 끊긴 것으로 본다


class QueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def value_of(num: Optional[float], flag: Optional[bool], txt: Optional[str], data_type: str = "Double") -> Any:
    if flag is not None:
        return flag
    if txt is not None:
        return txt
    if num is not None and not math.isfinite(num):
        return str(num)  # JSON 에는 NaN/Infinity 가 없다
    if num is not None and data_type in INTEGER_TYPES:
        return int(num)  # 정수 노드는 value_num(double)에 저장되지만 정수로 돌려준다
    return num


# ------------------------------------------------------------------ 설비·노드

async def get_equipment(session: AsyncSession, equipment_id: str) -> Optional[Equipment]:
    return await session.get(Equipment, equipment_id)


async def list_mappings(session: AsyncSession, equipment_id: Optional[str] = None) -> Sequence[AasNodeMapping]:
    stmt = select(AasNodeMapping).order_by(AasNodeMapping.mapping_id)
    if equipment_id:
        stmt = stmt.where(AasNodeMapping.equipment_id == equipment_id)
    return (await session.scalars(stmt)).all()


# ------------------------------------------------------------------ 이력

def encode_cursor(ts: datetime, node_pk: int) -> str:
    raw = json.dumps([ts.isoformat(), node_pk]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        ts, node_pk = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(ts), int(node_pk)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise QueryError("invalid_cursor", "cursor 형식이 올바르지 않습니다.") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise QueryError("timezone_required", "start/end 에는 시간대가 필요합니다 (예: 2026-11-02T10:00:00+09:00).")
    return value.astimezone(timezone.utc)


async def history(
    session: AsyncSession,
    equipment_id: str,
    node_ids: Sequence[str],
    start: datetime,
    end: Optional[datetime],
    *,
    mode: str = "auto",
    points: int = 600,
    bucket_seconds: Optional[float] = None,
    limit: int = 1000,
    cursor: Optional[str] = None,
) -> dict[str, Any]:
    start = _utc(start)
    end = _utc(end) if end else datetime.now(timezone.utc)
    if end <= start:
        raise QueryError("invalid_range", "end 는 start 보다 뒤여야 합니다.")
    if not node_ids:
        raise QueryError("node_required", "node_id 를 하나 이상 지정하세요.")
    if len(node_ids) > MAX_NODES:
        raise QueryError("too_many_nodes", f"node_id 는 최대 {MAX_NODES}개입니다.")
    span = end - start
    if mode == "auto":
        mode = "raw" if span <= timedelta(minutes=10) and bucket_seconds is None else "bucket"
    if mode == "raw" and span > MAX_RAW_RANGE:
        raise QueryError("range_too_large", "원시 조회는 최대 24시간입니다. mode=bucket 으로 조회하세요.")
    if span > MAX_BUCKET_RANGE:
        raise QueryError("range_too_large", "조회 기간은 최대 31일입니다.")

    nodes = (await session.scalars(
        select(EquipmentNode).where(EquipmentNode.equipment_id == equipment_id, EquipmentNode.node_id.in_(node_ids))
    )).all()
    missing = set(node_ids) - {n.node_id for n in nodes}
    if missing:
        raise QueryError("unknown_node", f"등록되지 않은 노드: {', '.join(sorted(missing))}")
    by_pk = {n.node_pk: n for n in nodes}
    pks = list(by_pk)
    summaries = await _summaries(session, pks, start, end)
    series = {pk: {"node_id": n.node_id, "node_name": n.node_name, "data_type": n.data_type, "unit": n.unit,
                   "summary": summaries.get(pk, {"count": 0})}
              for pk, n in by_pk.items()}

    if mode == "raw":
        limit = max(1, min(limit, MAX_RAW_LIMIT))
        cond = [OpcuaSample.node_pk.in_(pks), OpcuaSample.ts >= start, OpcuaSample.ts < end]
        if cursor:
            after_ts, after_pk = decode_cursor(cursor)
            cond.append(or_(OpcuaSample.ts > after_ts, and_(OpcuaSample.ts == after_ts, OpcuaSample.node_pk > after_pk)))
        rows = (await session.execute(
            select(OpcuaSample).where(*cond).order_by(OpcuaSample.ts, OpcuaSample.node_pk).limit(limit + 1)
        )).scalars().all()
        next_cursor = encode_cursor(rows[limit - 1].ts, rows[limit - 1].node_pk) if len(rows) > limit else None
        for pk in pks:
            series[pk]["samples"] = []
        for r in rows[:limit]:
            series[r.node_pk]["samples"].append({
                "ts": r.ts, "value": value_of(r.value_num, r.value_bool, r.value_text, by_pk[r.node_pk].data_type),
                "quality": r.quality,
                "source_ts": r.source_ts, "received_at": r.received_at,
            })
        return {"equipment_id": equipment_id, "start": start, "end": end, "mode": "raw", "bucket_seconds": None,
                "limit": limit, "next_cursor": next_cursor, "series": list(series.values())}

    points = max(10, min(points, MAX_POINTS))
    bucket = bucket_seconds or span.total_seconds() / points
    bucket = max(MIN_BUCKET_SECONDS, bucket)
    if span.total_seconds() / bucket > MAX_POINTS:
        raise QueryError("too_many_points", f"구간 수가 {MAX_POINTS} 를 넘습니다. bucket_seconds 를 늘리세요.")
    rows = (await session.execute(
        text("""
            SELECT node_pk,
                   time_bucket(make_interval(secs => :bucket), ts) AS bucket,
                   avg(value_num) AS avg, min(value_num) AS min, max(value_num) AS max,
                   last(value_num, ts) AS last_num, last(value_bool, ts) AS last_bool, last(value_text, ts) AS last_text,
                   count(*) AS n,
                   count(*) FILTER (WHERE quality >= 2147483648) AS bad
            FROM manufacturing.opcua_sample
            WHERE node_pk = ANY(:pks) AND ts >= :start AND ts < :end
            GROUP BY node_pk, bucket
            ORDER BY node_pk, bucket
        """).bindparams(bindparam("pks", type_=ARRAY(Integer)), bindparam("bucket", type_=Double)),
        {"bucket": bucket, "pks": pks, "start": start, "end": end},
    )).all()
    for pk in pks:
        series[pk]["buckets"] = []
    for r in rows:
        series[r.node_pk]["buckets"].append({
            "bucket": r.bucket, "avg": _finite(r.avg), "min": _finite(r.min), "max": _finite(r.max),
            "last": value_of(r.last_num, r.last_bool, r.last_text, by_pk[r.node_pk].data_type),
            "count": r.n, "bad_count": r.bad,
        })
    return {"equipment_id": equipment_id, "start": start, "end": end, "mode": "bucket", "bucket_seconds": bucket,
            "limit": None, "next_cursor": None, "series": list(series.values())}


def _finite(value: Optional[float]) -> Optional[float]:
    return value if value is None or math.isfinite(value) else None


async def _summaries(session: AsyncSession, pks: list[int], start: datetime, end: datetime) -> dict[int, dict]:
    rows = (await session.execute(
        select(OpcuaSample.node_pk, func.avg(OpcuaSample.value_num), func.min(OpcuaSample.value_num),
               func.max(OpcuaSample.value_num), func.count())
        .where(OpcuaSample.node_pk.in_(pks), OpcuaSample.ts >= start, OpcuaSample.ts < end)
        .group_by(OpcuaSample.node_pk)
    )).all()
    return {pk: {"avg": _finite(a), "min": _finite(mn), "max": _finite(mx), "count": n} for pk, a, mn, mx, n in rows}
