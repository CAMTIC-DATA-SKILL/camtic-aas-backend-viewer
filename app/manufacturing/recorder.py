"""기존 OPC-UA 구독(OpcUaSession) 수신값을 TimescaleDB 에 기록한다 (data_source = REAL).

구독 구조는 바꾸지 않는다. 세션의 수신 콜백에 리스너로 붙어 메모리 버퍼에 쌓고, 0.5초마다 배치로 저장한다.
어느 설비의 값인지는 DB 에 등록된 REAL 설비 중 opcua_endpoint 가 지금 접속한 서버와 같은 설비의 노드로 정한다.
노드별 실제 수신 간격(source timestamp 기준)을 재서 100ms 수집 여부를 확인할 수 있게 한다.
"""

from __future__ import annotations

import asyncio
import statistics
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import ValidationError
from sqlalchemy import select

from utils.log_setup import LogSetup

from ..opc_ua.client import DataChange
from ..opc_ua.values import serialize_value
from .ingest import ingest_batch
from .models import Equipment, EquipmentNode
from .schemas import IngestSample

logger = LogSetup.get_logger(__name__)

FLUSH_INTERVAL = 0.5
INDEX_REFRESH = 10.0
BUFFER_MAX = 100_000
INTERVAL_WINDOW = 200


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    # asyncua 는 UTC 시각을 시간대 없는 datetime 으로 준다.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class OpcUaRecorder:
    def __init__(self, runtime: Any, endpoint: Callable[[], Optional[str]]) -> None:
        self.runtime = runtime
        self._endpoint = endpoint
        self._index: dict[str, str] = {}          # 현장 NodeId → equipment_id
        self._buffer: deque[IngestSample] = deque()
        self._last_source: dict[str, datetime] = {}
        self._intervals: dict[str, deque[float]] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self.received = 0
        self.unmapped = 0
        self.inserted = 0
        self.duplicates = 0
        self.rejected = 0
        self.dropped = 0
        self.last_flush_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.indexed_endpoint: Optional[str] = None

    # ------------------------------------------------------------ 수신 콜백 (빠르게 끝나야 한다)

    def on_change(self, change: DataChange, received_at: datetime) -> None:
        self.received += 1
        equipment_id = self._index.get(change.node_id)
        if equipment_id is None or self._endpoint() != self.indexed_endpoint:
            self.unmapped += 1
            return
        source_ts = _utc(change.source_timestamp)
        if source_ts is not None:
            previous = self._last_source.get(change.node_id)
            if previous is not None and source_ts > previous:
                self._intervals.setdefault(change.node_id, deque(maxlen=INTERVAL_WINDOW)).append(
                    (source_ts - previous).total_seconds() * 1000)
            self._last_source[change.node_id] = source_ts
        try:
            sample = IngestSample(equipment_id=equipment_id, node_id=change.node_id,
                                  value=serialize_value(change.value), quality=change.status_code,
                                  source_ts=source_ts, received_at=received_at)
        except ValidationError:
            self.rejected += 1
            return
        if len(self._buffer) >= BUFFER_MAX:
            self._buffer.popleft()
            self.dropped += 1
        self._buffer.append(sample)

    # ------------------------------------------------------------ 저장 루프

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="opcua-recorder")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # 종료 시 남은 값 저장 시도. DB 가 내려가 있으면 포기하고 종료를 막지 않는다 (남은 건수는 로그로 남김).
        try:
            while self._buffer:
                await self._flush()
        except Exception as exc:
            logger.error("종료 중 미저장 %d건 폐기: %s", len(self._buffer), type(exc).__name__)

    async def _run(self) -> None:
        refreshed = -INDEX_REFRESH
        loop = asyncio.get_running_loop()
        while True:
            try:
                if loop.time() - refreshed >= INDEX_REFRESH or self._endpoint() != self.indexed_endpoint:
                    await self._refresh_index()
                    refreshed = loop.time()
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # DB 장애: 버퍼는 남겨 두고 다음 주기에 다시 시도
                self.last_error = type(exc).__name__
                logger.warning("OPC-UA 기록 실패 (버퍼 %d건 유지): %s", len(self._buffer), type(exc).__name__)
            await asyncio.sleep(FLUSH_INTERVAL)

    async def _refresh_index(self) -> None:
        endpoint = self._endpoint()
        index: dict[str, str] = {}
        if endpoint:
            async with self.runtime.sessions() as session:
                rows = await session.execute(
                    select(EquipmentNode.node_id, EquipmentNode.equipment_id)
                    .join(Equipment, Equipment.equipment_id == EquipmentNode.equipment_id)
                    .where(Equipment.data_source == "REAL", Equipment.enabled, EquipmentNode.enabled,
                           Equipment.opcua_endpoint == endpoint,
                           Equipment.equipment_id == self.runtime.cfg.equipment_id)
                )
                index = {r.node_id: r.equipment_id for r in rows}
        self._index = index
        self.indexed_endpoint = endpoint

    async def _flush(self) -> None:
        if not self._buffer:
            return
        batch = [self._buffer.popleft() for _ in range(min(len(self._buffer), 1000))]
        try:
            async with self.runtime.sessions() as session:
                result = await ingest_batch(session, self.runtime.nodes, batch, data_source="REAL")
        except BaseException:
            self._buffer.extendleft(reversed(batch))
            while len(self._buffer) > BUFFER_MAX:
                self._buffer.pop()
                self.dropped += 1
            raise
        self.inserted += result.inserted
        self.duplicates += result.duplicates
        self.rejected += result.rejected
        self.last_flush_at = datetime.now(timezone.utc)
        self.last_error = None

    # ------------------------------------------------------------ 상태

    def status(self) -> dict[str, Any]:
        per_node = {}
        all_intervals: list[float] = []
        for node_id, values in self._intervals.items():
            if values:
                ordered = sorted(values)
                per_node[node_id] = {"median_ms": round(statistics.median(ordered), 1),
                                     "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))], 1),
                                     "samples": len(ordered)}
                all_intervals.extend(ordered)
        return {
            "enabled": True,
            "endpoint": self.indexed_endpoint,
            "mapped_nodes": len(self._index),
            "received": self.received,
            "unmapped": self.unmapped,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "dropped": self.dropped,
            "pending": len(self._buffer),
            "last_flush_at": self.last_flush_at,
            "last_error": self.last_error,
            "interval_median_ms": round(statistics.median(all_intervals), 1) if all_intervals else None,
            "nodes_measured": len(per_node),
            "per_node": per_node,
        }
