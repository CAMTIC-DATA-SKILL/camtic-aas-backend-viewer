"""알람 자동 기록 (alarm_event). 2초마다 판정해 열고(ACTIVE) 닫는다(CLEARED, ended_at = 복구 시각).

- COMM_LOSS : 설비 단위 수신 중단 — 한 번이라도 수신했던 노드가 모두 끊김. 이때 노드 단위 STALE 은 닫는다.
- STALE     : 노드 단위 수신 중단 — 설비는 수신 중인데 그 노드만 끊김
- BAD_QUALITY / UNCERTAIN_QUALITY : 마지막 판정 이후 구간에 해당 품질이 있었거나 최신값이 그 품질
- THRESHOLD : data/manufacturing/alarm_rules.json 범위를 벗어난 값이 구간에 있었음
끊김 판정: 마지막 수신 후 max(5초, sampling_ms × 50) 이 지남. 한 번도 수신하지 않은 노드(현장 미지원 등)는 알람을 만들지 않는다.
중복 방지: 같은 (설비, 노드, 유형)의 열린 알람은 하나뿐이다 (alarm_event_open_uniq 부분 고유 인덱스).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import ARRAY, Double, Integer, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from config import BASE_DIR
from utils.log_setup import LogSetup

from .queries import STALE_FACTOR
from .values import quality_class

logger = LogSetup.get_logger(__name__)

RULES_FILE = BASE_DIR / "data" / "manufacturing" / "alarm_rules.json"
INTERVAL = 2.0
MIN_STALE = timedelta(seconds=5)


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    equipment_type: Optional[str]
    min: Optional[float]
    max: Optional[float]
    severity: str
    label: str


def load_rules(path: Path = RULES_FILE) -> list[Rule]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Rule(re.compile(r["match"]), r.get("equipment_type") or None, r.get("min"), r.get("max"),
                 r.get("severity", "WARNING"), r.get("label", r["match"])) for r in raw.get("rules", [])]


def rule_for(rules: list[Rule], equipment_type: str, node_name: str) -> Optional[Rule]:
    for rule in rules:
        if (rule.equipment_type is None or rule.equipment_type == equipment_type) and rule.pattern.search(node_name):
            return rule
    return None


_OPEN = text("""
    INSERT INTO manufacturing.alarm_event (equipment_id, node_id, alarm_type, severity, message, started_at)
    VALUES (:eq, :node, :type, :severity, :message, :started)
    ON CONFLICT (equipment_id, (coalesce(node_id, '')), alarm_type) WHERE status <> 'CLEARED' DO NOTHING
    RETURNING alarm_id
""")
_CLOSE = text("""
    UPDATE manufacturing.alarm_event
       SET status = 'CLEARED', ended_at = greatest(:ended, started_at)
     WHERE equipment_id = :eq AND coalesce(node_id, '') = coalesce(CAST(:node AS text), '')
       AND alarm_type = :type AND status <> 'CLEARED'
    RETURNING alarm_id
""")


class AlarmEngine:
    def __init__(self, runtime: Any, rules: Optional[list[Rule]] = None) -> None:
        self.runtime = runtime
        self.rules = load_rules() if rules is None else rules
        self._task: Optional[asyncio.Task] = None
        self._since: Optional[datetime] = None
        self.evaluations = 0
        self.opened = 0
        self.cleared = 0
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="alarm-engine")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self.evaluate()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # DB 장애 등: 다음 주기에 다시 판정
                self.last_error = type(exc).__name__
                logger.warning("알람 판정 실패: %s", type(exc).__name__)
            await asyncio.sleep(INTERVAL)

    async def evaluate(self, now: Optional[datetime] = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        since = self._since or now - timedelta(seconds=INTERVAL * 2)
        async with self.runtime.sessions() as session:
            changes = await self._evaluate(session, since, now)
            await session.commit()
        self._since = now
        self.evaluations += 1
        self.opened += changes["opened"]
        self.cleared += changes["cleared"]
        return changes

    async def _evaluate(self, session: AsyncSession, since: datetime, now: datetime) -> dict[str, int]:
        nodes = (await session.execute(text("""
            SELECT n.node_pk, n.equipment_id, n.node_id, n.node_name, n.unit, n.sampling_ms,
                   e.equipment_type, e.data_source, l.received_at, l.quality, l.value_num
            FROM manufacturing.equipment_node n
            JOIN manufacturing.equipment e USING (equipment_id)
            LEFT JOIN manufacturing.equipment_latest l ON l.node_pk = n.node_pk
            WHERE n.enabled AND e.enabled AND e.data_source = 'REAL' AND e.equipment_id = :equipment_id
        """), {"equipment_id": self.runtime.cfg.equipment_id})).all()
        rules = {r.node_pk: rule_for(self.rules, r.equipment_type, r.node_name) for r in nodes}
        window = await self._window(session, nodes, rules, since, now)
        opened = cleared = 0

        async def open_(eq: str, node: Optional[str], kind: str, severity: str, message: str, started: datetime):
            nonlocal opened
            row = (await session.execute(_OPEN, {"eq": eq, "node": node, "type": kind, "severity": severity,
                                                  "message": message, "started": started})).first()
            opened += row is not None

        async def close(eq: str, node: Optional[str], kind: str):
            nonlocal cleared
            cleared += len((await session.execute(_CLOSE, {"eq": eq, "node": node, "type": kind, "ended": now})).all())

        by_equipment: dict[str, list] = {}
        for r in nodes:
            by_equipment.setdefault(r.equipment_id, []).append(r)

        for equipment_id, rows in by_equipment.items():
            seen = [r for r in rows if r.received_at is not None]
            stale = [r for r in seen if now - r.received_at > max(MIN_STALE, timedelta(milliseconds=r.sampling_ms * STALE_FACTOR))]
            comm_loss = bool(seen) and len(stale) == len(seen)
            if comm_loss:
                last = max(r.received_at for r in seen)
                await open_(equipment_id, None, "COMM_LOSS", "CRITICAL",
                            f"{equipment_id} 데이터 수신 중단 (수신하던 노드 {len(seen)}개 모두)", last)
            else:
                await close(equipment_id, None, "COMM_LOSS")
            stale_ids = {r.node_pk for r in stale}
            for r in rows:
                if r.received_at is None:
                    continue  # 한 번도 수신하지 않은 노드(현장 미지원 등)는 노드 상태로만 보여 준다
                if not comm_loss and r.node_pk in stale_ids:
                    await open_(equipment_id, r.node_id, "STALE", "WARNING",
                                f"{r.node_name} 수신 중단", r.received_at)
                else:
                    await close(equipment_id, r.node_id, "STALE")

                w = window.get(r.node_pk, {})
                latest = quality_class(r.quality) if r.quality is not None else "NO_DATA"
                for kind, cls, severity, first in (("BAD_QUALITY", "BAD", "CRITICAL", w.get("bad_first")),
                                                   ("UNCERTAIN_QUALITY", "UNCERTAIN", "WARNING", w.get("unc_first"))):
                    if first is not None or latest == cls:
                        await open_(equipment_id, r.node_id, kind, severity,
                                    f"{r.node_name} 품질 {cls}", first or r.received_at)
                    if latest != cls:
                        await close(equipment_id, r.node_id, kind)

                rule = rules.get(r.node_pk)
                if rule is None:
                    continue
                if w.get("thr_first") is not None:
                    worst = w["thr_max"] if rule.max is not None and w["thr_max"] is not None and w["thr_max"] > rule.max else w["thr_min"]
                    await open_(equipment_id, r.node_id, "THRESHOLD", rule.severity,
                                f"{r.node_name} {rule.label} 초과: {worst:.3f}{r.unit or ''} "
                                f"(허용 {rule.min} ~ {rule.max})", w["thr_first"])
                elif r.value_num is not None and (rule.min is None or r.value_num >= rule.min) \
                        and (rule.max is None or r.value_num <= rule.max):
                    await close(equipment_id, r.node_id, "THRESHOLD")
        return {"opened": opened, "cleared": cleared}

    async def _window(self, session: AsyncSession, nodes, rules, since: datetime, until: datetime) -> dict[int, dict]:
        """since 이후 저장된 행에서 Bad/Uncertain 첫 시각과 임계값 위반 첫 시각·최대·최소 (노드별 PK 인덱스 범위)."""
        pks = [r.node_pk for r in nodes if r.received_at is not None]
        if not pks:
            return {}
        lo = [rules[pk].min if rules.get(pk) and rules[pk].min is not None else None for pk in pks]
        hi = [rules[pk].max if rules.get(pk) and rules[pk].max is not None else None for pk in pks]
        rows = (await session.execute(text("""
            SELECT b.node_pk,
                   min(s.ts) FILTER (WHERE s.quality >= 2147483648) AS bad_first,
                   min(s.ts) FILTER (WHERE s.quality >= 1073741824 AND s.quality < 2147483648) AS unc_first,
                   min(s.ts) FILTER (WHERE s.value_num < b.lo OR s.value_num > b.hi) AS thr_first,
                   max(s.value_num) FILTER (WHERE s.value_num < b.lo OR s.value_num > b.hi) AS thr_max,
                   min(s.value_num) FILTER (WHERE s.value_num < b.lo OR s.value_num > b.hi) AS thr_min
            FROM unnest(:pks, :lo, :hi) AS b(node_pk, lo, hi)
            CROSS JOIN LATERAL (
                SELECT ts, quality, value_num FROM manufacturing.opcua_sample s
                WHERE s.node_pk = b.node_pk AND s.ts > CAST(:since AS timestamptz)
                  AND s.ts <= CAST(:until AS timestamptz) + interval '5 seconds'
            ) s
            GROUP BY b.node_pk
        """).bindparams(bindparam("pks", type_=ARRAY(Integer)), bindparam("lo", type_=ARRAY(Double)),
                        bindparam("hi", type_=ARRAY(Double))),
            {"pks": pks, "lo": lo, "hi": hi, "since": since, "until": until})).mappings().all()
        return {r["node_pk"]: dict(r) for r in rows}

    def status(self) -> dict[str, Any]:
        return {"enabled": self._task is not None, "rules": len(self.rules), "evaluations": self.evaluations,
                "opened": self.opened, "cleared": self.cleared, "last_error": self.last_error}


async def list_alarms(session: AsyncSession, *, active_only: bool, equipment_id: Optional[str],
                      limit: int) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT a.alarm_id, a.equipment_id, e.equipment_name, e.data_source, a.node_id, n.node_name,
               a.alarm_type, a.severity, a.message, a.started_at, a.ended_at, a.status
        FROM manufacturing.alarm_event a
        JOIN manufacturing.equipment e USING (equipment_id)
        LEFT JOIN manufacturing.equipment_node n ON n.equipment_id = a.equipment_id AND n.node_id = a.node_id
        WHERE (NOT :active OR a.status <> 'CLEARED') AND (CAST(:eq AS text) IS NULL OR a.equipment_id = :eq)
        ORDER BY (a.status <> 'CLEARED') DESC, a.started_at DESC
        LIMIT :limit
    """), {"active": active_only, "eq": equipment_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def active_counts(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(text("""
        SELECT severity, count(*) AS n FROM manufacturing.alarm_event WHERE status <> 'CLEARED' GROUP BY severity
    """))).all()
    return {r.severity: r.n for r in rows}
