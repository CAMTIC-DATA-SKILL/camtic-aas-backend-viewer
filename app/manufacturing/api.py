"""/api/v1/timeseries — TimescaleDB 저장·조회·실시간 모니터링 API.

권한: 수신(POST /ingest)은 MFG_INGEST_API_KEY 가 반드시 필요하다.
      조회는 MFG_READ_API_KEY 를 설정했을 때만 X-API-Key(또는 SSE 의 api_key 쿼리)를 요구한다.
오류: {"detail": {"code": "...", "message": "..."}} 형식.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from asyncpg import InvalidAuthorizationSpecificationError, PostgresConnectionError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from config import ManufacturingConfig
from utils.log_setup import LogSetup

from . import alarms, monitor, queries
from .ingest import INGEST_STATS, NodeCache, ingest_batch
from .schemas import ErrorBody, HistoryResponse, IngestRequest, IngestResult

logger = LogSetup.get_logger(__name__)

router = APIRouter(prefix="/api/v1/timeseries", tags=["timeseries"])

_ERRORS = {
    400: {"model": ErrorBody, "description": "잘못된 요청"},
    401: {"model": ErrorBody, "description": "API 키 없음 또는 불일치"},
    404: {"model": ErrorBody, "description": "설비 없음"},
    503: {"model": ErrorBody, "description": "DB 비활성 또는 연결 실패"},
}
DB_ERRORS = (OperationalError, InterfaceError, OSError, TimeoutError, PoolTimeoutError,
             InvalidAuthorizationSpecificationError, PostgresConnectionError)
# SQLAlchemy 는 asyncpg 의 인증·DB 이름 오류(SQLSTATE 28P01, 3D000)를 일반 DBAPIError 로 감싼다.
# 연결 문제(08 연결, 28 인증, 3D DB 없음, 53300 연결 수 초과, 57P 서버 종료·기동 중)만 503 으로 보내고,
# 컬럼 오류 같은 코드 결함은 DB 장애처럼 숨기지 않고 500 으로 둔다.
CONNECTION_SQLSTATES = ("08", "28", "3D", "53300", "57P")
STREAM_INTERVAL = 1.0


def is_db_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, DB_ERRORS):
        return True
    if isinstance(exc, DBAPIError):
        return (getattr(exc.orig, "sqlstate", None) or "").startswith(CONNECTION_SQLSTATES)
    return False


@dataclass
class ManufacturingRuntime:
    cfg: ManufacturingConfig
    engine: AsyncEngine
    sessions: async_sessionmaker
    nodes: NodeCache
    recorder: Any = None     # OpcUaRecorder (MFG_RECORD_OPCUA)
    opcua: Any = None        # OpcUaSession (구독 설정 확인용)
    alarms: Any = None       # AlarmEngine


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def get_runtime(request: Request) -> ManufacturingRuntime:
    runtime: Optional[ManufacturingRuntime] = getattr(request.app.state, "manufacturing", None)
    if runtime is None:
        raise error(503, "disabled", "TimescaleDB 연동이 꺼져 있습니다 (.env 의 MFG_ENABLED=true 필요).")
    return runtime


async def get_session(runtime: ManufacturingRuntime = Depends(get_runtime)) -> AsyncIterator[AsyncSession]:
    async with runtime.sessions() as session:
        try:
            yield session
        except Exception as exc:
            # DB 가 꺼졌거나 연결·인증이 안 됨. 요청 오류(404 등)와 데이터 오류(제약 위반 등)는 그대로 올려 보낸다.
            if not is_db_unavailable(exc):
                raise
            logger.warning("TimescaleDB 연결 실패: %s", type(exc).__name__)
            raise error(503, "db_unavailable", "TimescaleDB 에 연결할 수 없습니다. DB 연결 설정을 확인하세요.") from exc


def _check_key(expected: Optional[str], given: Optional[str]) -> None:
    if not given or not secrets.compare_digest(expected.encode(), given.encode()):  # type: ignore[union-attr]
        raise error(401, "unauthorized", "X-API-Key 가 없거나 올바르지 않습니다.")


def require_read(
    runtime: ManufacturingRuntime = Depends(get_runtime),
    x_api_key: Optional[str] = Header(None),
    api_key: Optional[str] = Query(None, include_in_schema=False),  # EventSource 는 헤더를 못 보낸다
) -> None:
    if runtime.cfg.read_api_key:
        _check_key(runtime.cfg.read_api_key, x_api_key or api_key)


def require_ingest(
    runtime: ManufacturingRuntime = Depends(get_runtime), x_api_key: Optional[str] = Header(None)
) -> None:
    if not runtime.cfg.ingest_api_key:
        raise error(503, "ingest_disabled", "MFG_INGEST_API_KEY 가 설정되지 않아 수신을 받지 않습니다.")
    _check_key(runtime.cfg.ingest_api_key, x_api_key)


async def _equipment_or_404(session: AsyncSession, equipment_id: str):
    equipment = await queries.get_equipment(session, equipment_id)
    if equipment is None:
        raise error(404, "equipment_not_found", f"설비가 없습니다: {equipment_id}")
    return equipment


# ------------------------------------------------------------------ 수신

@router.post("/ingest", response_model=IngestResult, responses=_ERRORS, dependencies=[Depends(require_ingest)],
             summary="수집 시스템 → 시계열 저장 (배치, 멱등)")
async def ingest(
    body: IngestRequest,
    runtime: ManufacturingRuntime = Depends(get_runtime),
    session: AsyncSession = Depends(get_session),
) -> IngestResult:
    """커밋 후 응답한다. 같은 배치를 다시 보내도 (node, ts) 중복은 저장되지 않는다."""
    return await ingest_batch(session, runtime.nodes, body.samples, data_source=body.data_source)


# ------------------------------------------------------------------ 조회

read = APIRouter(dependencies=[Depends(require_read)], responses=_ERRORS)


@read.get("/summary", summary="요약: 설비·노드 수, 최근 1분 수집 건수, 목표 대비 실제 초당 수집량")
async def ts_summary(session: AsyncSession = Depends(get_session)):
    result = await monitor.summary(session)
    result["active_alarms"] = await alarms.active_counts(session)
    return result


@read.get("/equipment", summary="설비 목록 (data_source, 노드 수, 마지막 수신)")
async def ts_equipment(session: AsyncSession = Depends(get_session)):
    return await monitor.equipment_overview(session)


@read.get("/nodes", summary="노드 목록 + 최신값 + AAS 매핑")
async def ts_nodes(
    equipment_id: Optional[str] = None,
    data_source: Optional[str] = Query(None, pattern="^REAL$", description="출처 필터 (이 앱은 REAL 설비만 저장)"),
    session: AsyncSession = Depends(get_session),
):
    return await monitor.nodes(session, equipment_id, data_source)


@read.get("/node-status", summary="노드 상태: 수신 중 / 미수집 / 현장 미지원 / 데이터 없음 + 실제 수집 간격")
async def ts_node_status(
    equipment_id: str,
    runtime: ManufacturingRuntime = Depends(get_runtime),
    session: AsyncSession = Depends(get_session),
):
    await _equipment_or_404(session, equipment_id)
    opcua = runtime.opcua.status() if runtime.opcua is not None else {}
    return await monitor.node_status(session, equipment_id, subscription=opcua.get("subscription"),
                                     subscribed_endpoint=opcua.get("endpoint") if opcua.get("connected") else None)


@read.get("/latest", summary="노드별 최신값 (equipment_latest)")
async def ts_latest(
    equipment_id: Optional[str] = None,
    node_id: Optional[list[str]] = Query(None),
    data_source: Optional[str] = Query(None, pattern="^REAL$", description="출처 필터 (이 앱은 REAL 설비만 저장)"),
    session: AsyncSession = Depends(get_session),
):
    rows = await monitor.nodes(session, equipment_id, data_source)
    if node_id:
        wanted = set(node_id)
        rows = [r for r in rows if r["node_id"] in wanted]
    return rows


@read.get("/history", response_model=HistoryResponse, summary="기간 이력 (원시·다운샘플, avg/min/max)")
async def ts_history(
    equipment_id: str,
    node_id: list[str] = Query(..., description="노드 ID (여러 개 가능, 최대 20)"),
    start: datetime = Query(..., description="시작 (포함, 시간대 필수)"),
    end: Optional[datetime] = Query(None, description="끝 (미포함, 기본 현재)"),
    mode: str = Query("auto", pattern="^(auto|raw|bucket)$",
                      description="auto: 10분 이하면 raw, 넘으면 bucket(다운샘플)"),
    points: int = Query(600, ge=10, le=queries.MAX_POINTS, description="bucket 모드 노드당 최대 점 수"),
    bucket_seconds: Optional[float] = Query(None, ge=queries.MIN_BUCKET_SECONDS),
    limit: int = Query(1000, ge=1, le=queries.MAX_RAW_LIMIT, description="raw 모드 페이지 크기"),
    cursor: Optional[str] = Query(None, description="이전 응답의 next_cursor"),
    session: AsyncSession = Depends(get_session),
):
    equipment = await _equipment_or_404(session, equipment_id)
    try:
        result = await queries.history(session, equipment_id, node_id, start, end, mode=mode, points=points,
                                       bucket_seconds=bucket_seconds, limit=limit, cursor=cursor)
        return {**result, "data_source": equipment.data_source}
    except queries.QueryError as exc:
        raise error(400, exc.code, str(exc)) from exc


@read.get("/history.csv", summary="기간 이력 CSV (원시 최대 24시간·노드당 10,000행, 넘으면 다운샘플)")
async def ts_history_csv(
    equipment_id: str,
    node_id: list[str] = Query(...),
    start: datetime = Query(...),
    end: Optional[datetime] = Query(None),
    mode: str = Query("auto", pattern="^(auto|raw|bucket)$"),
    session: AsyncSession = Depends(get_session),
):
    equipment = await _equipment_or_404(session, equipment_id)
    source = equipment.data_source
    try:
        data = await queries.history(session, equipment_id, node_id, start, end, mode=mode,
                                     limit=queries.MAX_RAW_LIMIT, points=queries.MAX_POINTS)
    except queries.QueryError as exc:
        raise error(400, exc.code, str(exc)) from exc
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if data["mode"] == "raw":
        writer.writerow(["equipment_id", "data_source", "node_id", "node_name", "unit", "ts", "value", "quality",
                         "source_ts", "received_at"])
        for series in data["series"]:
            for s in series["samples"]:
                writer.writerow([equipment_id, source, series["node_id"], series["node_name"], series["unit"] or "",
                                 s["ts"].isoformat(), s["value"], s["quality"],
                                 s["source_ts"].isoformat() if s["source_ts"] else "", s["received_at"].isoformat()])
    else:
        writer.writerow(["equipment_id", "data_source", "node_id", "node_name", "unit", "bucket_start",
                         "bucket_seconds", "avg", "min", "max", "last", "count", "bad_count"])
        for series in data["series"]:
            for b in series["buckets"]:
                writer.writerow([equipment_id, source, series["node_id"], series["node_name"], series["unit"] or "",
                                 b["bucket"].isoformat(), data["bucket_seconds"], b["avg"], b["min"], b["max"],
                                 b["last"], b["count"], b["bad_count"]])
    name = f"{equipment_id}_{data['start']:%Y%m%dT%H%M%S}_{data['end']:%Y%m%dT%H%M%S}_{data['mode']}.csv"
    return StreamingResponse(iter(["﻿" + buffer.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@read.get("/recent", summary="DB 에 저장된 최신 행 (최신순, 폴링용)")
async def ts_recent(
    equipment_id: Optional[str] = None,
    node_id: Optional[str] = None,
    after: Optional[datetime] = Query(None, description="이 시각(ts) 이후만"),
    data_source: Optional[str] = Query(None, pattern="^REAL$", description="출처 필터 (이 앱은 REAL 설비만 저장)"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    rows = await monitor.recent_rows(session, equipment_id=equipment_id, node_id=node_id, after=after,
                                     data_source=data_source, limit=limit)
    return {"rows": rows, "cursor": max((r["ts"] for r in rows), default=after)}


@read.get("/stream", summary="실시간 스트림 (SSE). 1초마다 새로 저장된 행을 보낸다")
async def ts_stream(
    request: Request,
    equipment_id: Optional[str] = None,
    node_id: Optional[str] = None,
    data_source: Optional[str] = Query(None, pattern="^REAL$", description="출처 필터 (이 앱은 REAL 설비만 저장)"),
    limit: int = Query(200, ge=1, le=500),
    max_events: Optional[int] = Query(None, ge=1, description="이만큼 보내고 끝낸다 (curl 점검·테스트용)"),
    runtime: ManufacturingRuntime = Depends(get_runtime),
):
    """100ms 저장과 화면 갱신을 분리한다: 서버가 1초마다 DB 에서 새 행만 읽어 한 번에 보낸다."""

    async def events() -> AsyncIterator[str]:
        cursors: dict[int, datetime] = {}  # 노드별 마지막으로 보낸 ts (설비 시계 차이에 영향받지 않게)
        sent = 0
        while not await request.is_disconnected():
            if max_events is not None and sent >= max_events:
                return
            sent += 1
            payload: dict[str, Any]
            try:
                async with runtime.sessions() as session:
                    rows = await monitor.recent_rows(session, equipment_id=equipment_id, node_id=node_id,
                                                     after_by_node=cursors, data_source=data_source, limit=limit)
                for row in rows:
                    if row["node_pk"] not in cursors or row["ts"] > cursors[row["node_pk"]]:
                        cursors[row["node_pk"]] = row["ts"]
                payload = {"rows": rows, "db_connected": True}
            except Exception as exc:
                if not is_db_unavailable(exc):
                    raise
                payload = {"rows": [], "db_connected": False, "error": "db_unavailable"}
            payload["server_time"] = datetime.now(timezone.utc)
            yield f"data: {json.dumps(jsonable_encoder(payload), ensure_ascii=False)}\n\n"
            if max_events is None or sent < max_events:
                await asyncio.sleep(STREAM_INTERVAL)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@read.get("/db-status", summary="DB·TimescaleDB·하이퍼테이블 상태, 저장량, 수신 지연, 품질, OPC-UA 기록 상태")
async def ts_db_status(runtime: ManufacturingRuntime = Depends(get_runtime)):
    try:
        async with runtime.sessions() as session:
            status = await monitor.db_status(session)
            status["storage"] = await monitor.storage(session)
    except Exception as exc:
        if not is_db_unavailable(exc):
            raise
        status = {"db_connected": False, "error": "db_unavailable"}
    status["ingest_performance"] = INGEST_STATS.snapshot()
    status["alarm_engine"] = runtime.alarms.status() if runtime.alarms else {"enabled": False}
    status["opcua_recorder"] = runtime.recorder.status() if runtime.recorder else {"enabled": False}
    if runtime.opcua is not None:
        opcua = runtime.opcua.status()
        status["opcua_subscription"] = {
            "connected": opcua.get("connected"), "endpoint": opcua.get("endpoint"),
            "package": opcua.get("subscribePackageId"), "subscribed_nodes": len(opcua.get("subscribedNodeIds") or []),
            "receiving_nodes": opcua.get("latestCount"), **(opcua.get("subscription") or {}),
        }
    return status


@read.get("/mapping", summary="AAS Property(Submodel id + idShort 경로) → 연결된 TimescaleDB 노드")
async def ts_mapping_lookup(
    submodel_id: str, path: str, session: AsyncSession = Depends(get_session)
):
    return {"submodel_id": submodel_id, "path": path,
            "nodes": await monitor.mapping_lookup(session, submodel_id, path)}


@read.get("/alarms", summary="알람 (진행 중 우선, 최신순)")
async def ts_alarms(
    status: str = Query("all", pattern="^(active|all)$"),
    equipment_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
):
    return {"alarms": await alarms.list_alarms(session, active_only=status == "active", equipment_id=equipment_id,
                                               limit=limit),
            "active": await alarms.active_counts(session)}


@read.get("/mappings", summary="AAS ↔ 노드 매핑 목록")
async def ts_mappings(equipment_id: Optional[str] = None, session: AsyncSession = Depends(get_session)):
    rows = await queries.list_mappings(session, equipment_id)
    return [{"mapping_id": m.mapping_id, "equipment_id": m.equipment_id, "node_id": m.node_id,
             "aas_submodel_id": m.aas_submodel_id, "aas_id_short_path": m.aas_id_short_path,
             "mapping_type": m.mapping_type} for m in rows]



router.include_router(read)
