"""수신 배치 저장: 검증 → opcua_sample INSERT(중복 무시) → equipment_latest UPSERT, 한 트랜잭션.

- 중복: PK (node_pk, ts) + ON CONFLICT DO NOTHING. 수집 시스템이 같은 배치를 다시 보내도 안전하다 (멱등).
- 순서 역전: 최신값보다 과거 시각인 샘플은 저장하되 equipment_latest 는 덮어쓰지 않는다.
- 응답은 커밋 뒤에 돌아가므로, 수집 시스템은 200 을 받은 배치만 "저장됨"으로 보면 된다.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from utils.log_setup import LogSetup

from .models import Equipment, EquipmentLatest, EquipmentNode, OpcuaSample
from .schemas import IngestRejected, IngestResult, IngestSample
from .values import ValueRejected, normalize, quality_class

logger = LogSetup.get_logger(__name__)

# 수집 시스템 시계가 이만큼 미래면 거부한다 (시각 동기화 오류).
MAX_FUTURE = timedelta(minutes=5)
MAX_REJECTED_DETAIL = 100
NODE_CACHE_REFRESH_MIN = 5.0


@dataclass(frozen=True)
class NodeRef:
    node_pk: int
    data_type: str
    enabled: bool
    data_source: str  # 설비의 데이터 출처


class NodeCache:
    """(equipment_id, node_id) → node_pk. 모르는 노드가 오면 최대 5초에 한 번 다시 읽는다."""

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, str], NodeRef] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self, session: AsyncSession, *, force: bool = False) -> None:
        async with self._lock:
            if not force and time.monotonic() - self._loaded_at < NODE_CACHE_REFRESH_MIN:
                return
            rows = await session.execute(
                select(EquipmentNode.equipment_id, EquipmentNode.node_id, EquipmentNode.node_pk,
                       EquipmentNode.data_type, (EquipmentNode.enabled & Equipment.enabled).label("enabled"),
                       Equipment.data_source)
                .join(Equipment, Equipment.equipment_id == EquipmentNode.equipment_id)
            )
            self._nodes = {(r.equipment_id, r.node_id): NodeRef(r.node_pk, r.data_type, r.enabled, r.data_source)
                           for r in rows}
            self._loaded_at = time.monotonic()

    def get(self, equipment_id: str, node_id: str) -> Optional[NodeRef]:
        return self._nodes.get((equipment_id, node_id))

    def by_source(self, data_source: str) -> dict[tuple[str, str], NodeRef]:
        return {key: ref for key, ref in self._nodes.items() if ref.data_source == data_source and ref.enabled}

    def invalidate(self) -> None:
        self._loaded_at = 0.0


class IngestStats:
    """배치 저장 시간 (장기 실행 시 적재 성능 확인용). 최근 2,000배치만 들고 있는다."""

    def __init__(self) -> None:
        self.recent: deque[tuple[float, float, int]] = deque(maxlen=2000)  # (끝난 시각, ms, 행 수)
        self.batches = 0
        self.rows = 0

    def record(self, ms: float, rows: int) -> None:
        self.recent.append((time.time(), ms, rows))
        self.batches += 1
        self.rows += rows

    def snapshot(self) -> dict:
        if not self.recent:
            return {"batches": self.batches, "rows": self.rows}
        ms = sorted(x[1] for x in self.recent)
        span = self.recent[-1][0] - self.recent[0][0]
        rows = sum(x[2] for x in self.recent)
        return {
            "batches": self.batches, "rows": self.rows,
            "recent_batches": len(ms),
            "batch_ms_p50": round(ms[len(ms) // 2], 1), "batch_ms_p95": round(ms[int(0.95 * (len(ms) - 1))], 1),
            "batch_ms_max": round(ms[-1], 1),
            "rows_per_s": round(rows / span, 1) if span > 0 else None,
            "db_busy_ratio": round(sum(ms) / 1000 / span, 3) if span > 0 else None,
        }


INGEST_STATS = IngestStats()


@dataclass
class _Row:
    index: int
    equipment_id: str
    node_id: str
    node_pk: int
    ts: datetime
    value_num: Optional[float]
    value_bool: Optional[bool]
    value_text: Optional[str]
    quality: int
    source_ts: Optional[datetime]
    received_at: datetime


def _check_tz(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueRejected("timezone_required")
    return value.astimezone(timezone.utc)


async def ingest_batch(
    session: AsyncSession,
    cache: NodeCache,
    samples: Sequence[IngestSample],
    *,
    data_source: str = "REAL",
    now: Optional[datetime] = None,
) -> IngestResult:
    """활성화된 REAL 설비 노드만 저장한다."""
    started = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    rows: list[_Row] = []
    rejected: list[IngestRejected] = []
    rejected_count = 0
    await cache.refresh(session)
    refreshed = False

    for index, sample in enumerate(samples):
        ref = cache.get(sample.equipment_id, sample.node_id)
        if ref is None and not refreshed:
            await cache.refresh(session)
            refreshed = True
            ref = cache.get(sample.equipment_id, sample.node_id)
        try:
            if ref is None:
                raise ValueRejected("unknown_node")
            if not ref.enabled:
                raise ValueRejected("node_disabled")
            if data_source != "REAL" or ref.data_source != "REAL":
                raise ValueRejected("data_source_mismatch")
            source_ts = _check_tz(sample.source_ts)
            received_at = _check_tz(sample.received_at) or now
            ts = source_ts or received_at
            if ts > now + MAX_FUTURE:
                raise ValueRejected("future_timestamp")
            value_num, value_bool, value_text = normalize(ref.data_type, sample.value)
        except ValueRejected as exc:
            rejected_count += 1
            if len(rejected) < MAX_REJECTED_DETAIL:
                rejected.append(IngestRejected(index=index, equipment_id=sample.equipment_id,
                                               node_id=sample.node_id, reason=str(exc)))
            continue
        rows.append(_Row(index, sample.equipment_id, sample.node_id, ref.node_pk, ts,
                         value_num, value_bool, value_text, sample.quality, source_ts, received_at))

    # 같은 배치 안의 같은 (node, ts) 는 첫 번째만 쓴다.
    unique: dict[tuple[int, datetime], _Row] = {}
    for row in rows:
        unique.setdefault((row.node_pk, row.ts), row)
    batch = list(unique.values())

    inserted = 0
    late = 0
    if batch:
        node_pks = {row.node_pk for row in batch}
        current = dict(
            (await session.execute(
                select(EquipmentLatest.node_pk, EquipmentLatest.last_ts).where(EquipmentLatest.node_pk.in_(node_pks))
            )).all()
        )
        # executemany + RETURNING: SQLAlchemy 가 파라미터 한도에 맞춰 여러 VALUES 문장으로 나눠 보낸다.
        result = await session.execute(
            insert(OpcuaSample).on_conflict_do_nothing(index_elements=["node_pk", "ts"])
            .returning(OpcuaSample.node_pk, OpcuaSample.ts),
            [
                {"ts": r.ts, "node_pk": r.node_pk, "value_num": r.value_num, "value_bool": r.value_bool,
                 "value_text": r.value_text, "quality": r.quality, "source_ts": r.source_ts,
                 "received_at": r.received_at}
                for r in batch
            ],
        )
        new_rows = result.all()
        inserted = len(new_rows)
        # 순서 역전: 새로 저장된 행 중 이미 받은 최신값보다 과거인 것 (중복 재전송은 세지 않는다)
        late = sum(1 for node_pk, ts in new_rows if node_pk in current and ts < current[node_pk])

        newest: dict[int, _Row] = {}
        inserted_keys = set(new_rows)
        for row in batch:
            if (row.node_pk, row.ts) not in inserted_keys:
                continue
            best = newest.get(row.node_pk)
            if best is None or row.ts > best.ts:
                newest[row.node_pk] = row
        if not newest:
            await session.commit()
            INGEST_STATS.record((time.perf_counter() - started) * 1000, inserted)
            return IngestResult(received=len(samples), inserted=inserted, duplicates=len(rows) - inserted,
                                late=late, bad_quality=sum(quality_class(r.quality) == "BAD" for r in rows),
                                rejected=rejected_count, rejected_samples=rejected)
        stmt = insert(EquipmentLatest).values([
            {"equipment_id": r.equipment_id, "node_id": r.node_id, "node_pk": r.node_pk, "last_ts": r.ts,
             "value_num": r.value_num, "value_bool": r.value_bool, "value_text": r.value_text,
             "quality": r.quality, "source_ts": r.source_ts, "received_at": r.received_at}
            for r in newest.values()
        ])
        excluded = stmt.excluded
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["equipment_id", "node_id"],
                set_={
                    "last_ts": excluded.last_ts, "value_num": excluded.value_num,
                    "value_bool": excluded.value_bool, "value_text": excluded.value_text,
                    "quality": excluded.quality, "source_ts": excluded.source_ts,
                    "received_at": excluded.received_at, "updated_at": now,
                },
                # 늦게 온 과거 데이터는 최신값을 덮어쓰지 않는다.
                where=excluded.last_ts > EquipmentLatest.last_ts,
            )
        )
    await session.commit()
    INGEST_STATS.record((time.perf_counter() - started) * 1000, inserted)

    return IngestResult(
        received=len(samples),
        inserted=inserted,
        duplicates=len(rows) - inserted,
        late=late,
        bad_quality=sum(1 for row in rows if quality_class(row.quality) == "BAD"),
        rejected=rejected_count,
        rejected_samples=rejected,
    )
