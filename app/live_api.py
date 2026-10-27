from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .live_frame import collector_data_url, frame_from_collector, frame_from_mapping, http_get_json
from .live_record import LiveRecorder
from .opcua_export import load_engineering_nodes
from .opcua_mapping import apply_engineering, apply_mapping, load_mapping
from .repository import AasxRepository

router = APIRouter(prefix="/api/live", tags=["live"])


class RecordBody(BaseModel):
    on: bool
    packageId: Optional[str] = None
    source: str = "opcua"
    collectorUrl: Optional[str] = None


def get_repository(request: Request) -> AasxRepository:
    return request.app.state.repository


def get_recorder(request: Request) -> LiveRecorder:
    return request.app.state.recorder


def get_opcua(request: Request):
    return request.app.state.opcua


@router.get("/status")
def live_status(
    request: Request,
    recorder: LiveRecorder = Depends(get_recorder),
) -> dict[str, Any]:
    default_url = request.app.state.collector_url
    return {
        "collectorUrl": default_url or "",
        "record": recorder.status(),
    }


@router.get("/collector")
async def live_collector(url: str, seq: Optional[str] = None) -> Any:
    try:
        target = collector_data_url(url)
        payload = await asyncio.to_thread(http_get_json, target)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    frame = frame_from_collector(payload)
    if seq is not None and seq != "" and str(frame.get("seq") or "") == seq:
        return Response(status_code=204)
    return {"source": "collector", "frame": frame, "raw": payload}


@router.get("/record")
def record_status(recorder: LiveRecorder = Depends(get_recorder)) -> dict[str, Any]:
    return recorder.status()


@router.post("/record")
async def record_toggle(
    body: RecordBody,
    request: Request,
    recorder: LiveRecorder = Depends(get_recorder),
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    if not body.on:
        return await recorder.set(False)
    source = (body.source or "opcua").strip().lower()
    if source not in {"opcua", "collector"}:
        raise HTTPException(status_code=400, detail="source 는 opcua 또는 collector 여야 합니다.")
    package_id = (body.packageId or "").strip()
    if source == "opcua" and not package_id:
        raise HTTPException(status_code=400, detail="OPC-UA 기록에는 packageId 가 필요합니다.")
    try:
        fetcher = _make_fetcher(
            request,
            source=source,
            package_id=package_id,
            collector_url=body.collectorUrl,
            repository=repository,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await recorder.set(True, fetcher=fetcher, package_id=package_id, source=source)


def _make_fetcher(
    request: Request,
    *,
    source: str,
    package_id: str,
    collector_url: Optional[str],
    repository: AasxRepository,
):
    if source == "collector":
        url = collector_data_url(collector_url or request.app.state.collector_url or "")

        async def fetch_collector() -> dict[str, Any]:
            payload = await asyncio.to_thread(http_get_json, url)
            return frame_from_collector(payload)

        return fetch_collector

    package = repository.get(package_id)
    if package is None:
        raise ValueError(f"Package '{package_id}' not found")
    mapping = load_mapping(package.id, repository.artifact_dir(package.id))
    nodes = load_engineering_nodes(repository.artifact_dir(package.id), variables_only=True)
    if mapping is None and not nodes:
        raise ValueError("AAS↔OPC-UA 매핑 또는 engineering.csv 변수 노드가 없습니다.")
    session = request.app.state.opcua
    node_ids = mapping.required_node_ids() if mapping is not None else [
        str(node["nodeId"]) for node in nodes if node.get("nodeId")
    ]
    aas_id = (mapping.aas_id if mapping else "") or (
        str(package.shells[0].id) if package.shells else ""
    )

    async def fetch_opcua() -> dict[str, Any]:
        cached = session.latest_values()
        values = cached if cached else await session.read(node_ids)
        mapped = (
            apply_mapping(mapping, values)
            if mapping is not None
            else apply_engineering(nodes, values, package_id=package.id)
        )
        return frame_from_mapping(
            mapped,
            connected=session.is_connected,
            package_id=package.id,
            aas_id=aas_id,
        )

    return fetch_opcua
