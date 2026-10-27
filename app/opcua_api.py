from pathlib import Path
from typing import Any, Optional

from asyncua import ua
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.log_setup import LogSetup

from .live_frame import frame_from_mapping
from .opc_ua import OpcUaClientError, OpcUaSession
from .opcua_catalog import CatalogServer, load_catalog
from .opcua_export import load_engineering_nodes
from .opcua_mapping import AasOpcUaMapping, apply_engineering, apply_mapping, load_mapping
from .repository import AasxPackage, AasxRepository

logger = LogSetup.get_logger(__name__)

router = APIRouter(prefix="/api/opcua", tags=["opcua"])


class ConnectBody(BaseModel):
    serverId: Optional[str] = None
    aasId: Optional[str] = None
    packageId: Optional[str] = None
    endpoint: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    securityPolicy: Optional[str] = None
    securityMode: Optional[str] = None


class ReadBody(BaseModel):
    nodeIds: list[str] = Field(default_factory=list)


class WriteBody(BaseModel):
    nodeId: str
    value: Any


class SubscribeBody(BaseModel):
    nodeIds: list[str] = Field(default_factory=list)


def get_repository(request: Request) -> AasxRepository:
    return request.app.state.repository


def get_opcua(request: Request) -> OpcUaSession:
    return request.app.state.opcua


def get_package(package_id: str, repository: AasxRepository = Depends(get_repository)) -> AasxPackage:
    package = repository.get(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"Package '{package_id}' not found")
    return package


def mapping_live_response(
    mapping: AasOpcUaMapping,
    session: OpcUaSession,
    package_id: str,
) -> dict[str, Any]:
    mapped = apply_mapping(mapping, session.latest_values())
    return {
        "packageId": package_id,
        "source": "subscription",
        "mappingSource": mapping.source,
        "catalogServerId": session.catalog_server_id,
        "subscribePackageId": session.subscribe_package_id,
        "subscribedNodeIds": session.subscribed_node_ids,
        "latestCount": len(session.latest_values()),
        "values": mapped,
        "frame": frame_from_mapping(
            mapped,
            connected=session.is_connected,
            package_id=package_id,
            aas_id=mapping.aas_id,
        ),
    }


def engineering_live_response(
    nodes: list[dict[str, Any]],
    session: OpcUaSession,
    package_id: str,
    aas_id: str = "",
) -> dict[str, Any]:
    mapped = apply_engineering(nodes, session.latest_values(), package_id=package_id)
    return {
        "packageId": package_id,
        "source": "engineering",
        "catalogServerId": session.catalog_server_id,
        "subscribePackageId": session.subscribe_package_id,
        "subscribedNodeIds": session.subscribed_node_ids,
        "latestCount": len(session.latest_values()),
        "values": mapped,
        "frame": frame_from_mapping(
            mapped,
            connected=session.is_connected,
            package_id=package_id,
            aas_id=aas_id,
        ),
    }


def _require_mapping(package: AasxPackage, repository: AasxRepository) -> AasOpcUaMapping:
    mapping = load_mapping(package.id, repository.artifact_dir(package.id))
    if mapping is None:
        raise HTTPException(
            status_code=404,
            detail="AAS↔OPC-UA 매핑이 없습니다. data/mappings/{package_id}.json 을 추가하세요.",
        )
    return mapping


def _package_live_source(
    package: AasxPackage, repository: AasxRepository
) -> tuple[str, Optional[AasOpcUaMapping], list[dict[str, Any]], list[str], str]:
    mapping = load_mapping(package.id, repository.artifact_dir(package.id))
    aas_id = mapping.aas_id if mapping else ""
    if not aas_id and package.shells:
        aas_id = str(package.shells[0].id)
    if mapping is not None:
        return "mapping", mapping, [], mapping.required_node_ids(), aas_id
    nodes = load_engineering_nodes(repository.artifact_dir(package.id), variables_only=True)
    node_ids = [str(node["nodeId"]) for node in nodes if node.get("nodeId")]
    return "engineering", None, nodes, node_ids, aas_id


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, OpcUaClientError):
        status = 503 if "연결되어 있지" in str(exc) else 502
        return HTTPException(status_code=status, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ua.UaError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.get("/status")
def opcua_status(session: OpcUaSession = Depends(get_opcua)) -> dict[str, Any]:
    return session.status()


@router.get("/catalog")
def opcua_catalog() -> dict[str, Any]:
    try:
        return load_catalog().to_dict()
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/connect")
async def opcua_connect(
    body: ConnectBody | None = None,
    session: OpcUaSession = Depends(get_opcua),
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    payload = body or ConnectBody()
    try:
        return await connect_from_catalog(
            session,
            repository,
            server_id=payload.serverId,
            aas_id=payload.aasId,
            package_id=payload.packageId,
            endpoint=payload.endpoint,
            username=payload.username,
            password=payload.password,
            security_policy=payload.securityPolicy,
            security_mode=payload.securityMode,
            require_server=bool(payload.serverId),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise _http_error(exc) from exc


async def connect_from_catalog(
    session: OpcUaSession,
    repository: AasxRepository,
    *,
    server_id: Optional[str] = None,
    aas_id: Optional[str] = None,
    package_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    security_policy: Optional[str] = None,
    security_mode: Optional[str] = None,
    require_server: bool = False,
    use_default_server: bool = False,
) -> dict[str, Any]:
    server: CatalogServer | None = None
    if server_id or use_default_server:
        catalog = load_catalog()
        server = catalog.get(server_id) if server_id else catalog.default_server()
        if server is None and require_server:
            raise LookupError(f"카탈로그 서버 '{server_id}' 가 없습니다.")
        if server is None and use_default_server:
            raise LookupError("OPC-UA 카탈로그에 기본 서버가 없습니다.")

    await session.connect(
        endpoint=endpoint or (server.endpoint if server else None),
        username=username,
        password=password,
        security_policy=security_policy or (server.security_policy if server else None),
        security_mode=security_mode or (server.security_mode if server else None),
        catalog_server_id=server.id if server else "",
    )
    resolved_aas = (aas_id or (server.aas_id if server else "") or "").strip()
    subscribe_error = await subscribe_engineering(
        session, repository, aas_id=resolved_aas, package_id=(package_id or "").strip()
    )
    status = session.status()
    try:
        status["namespaces"] = await session.namespaces()
    except Exception:
        status["namespaces"] = []
    logger.info(
        "OPC-UA 토픽 등록: endpoint=%s server=%s aasId=%s count=%d",
        status.get("endpoint"),
        status.get("catalogServerId") or "-",
        resolved_aas or "-",
        len(status.get("subscribedNodeIds") or []),
    )
    if subscribe_error:
        status["subscribeError"] = subscribe_error
        logger.warning("OPC-UA 매핑 구독 실패: %s", subscribe_error)
    return status


async def subscribe_engineering(
    session: OpcUaSession,
    repository: AasxRepository,
    aas_id: str = "",
    package_id: str = "",
) -> Optional[str]:
    package = None
    if aas_id:
        package = repository.find_by_shell_id(aas_id)
        if package is None:
            return f"AAS id '{aas_id}' 에 해당하는 패키지가 없습니다."
    elif package_id:
        package = repository.get(package_id)
        if package is None:
            return f"Package '{package_id}' not found"
    if package is None:
        logger.info("OPC-UA 토픽 등록 생략: aasId/packageId 가 없습니다.")
        return None
    source, _mapping, nodes, node_ids, _aas_id = _package_live_source(package, repository)
    if not node_ids:
        if source == "engineering" and not nodes:
            return f"engineering.csv 변수 노드가 없습니다: {package.id}"
        return f"구독할 NodeId 가 없습니다: {package.id}"
    try:
        await session.ensure_subscribed(node_ids, package_id=package.id)
    except Exception as exc:
        logger.warning(
            "OPC-UA 토픽 등록 실패: aasId=%s package=%s (%s)",
            aas_id or "-",
            package.id,
            exc,
        )
        return str(exc)
    logger.info(
        "OPC-UA 토픽 등록: aasId=%s package=%s count=%d source=%s",
        aas_id or "-",
        package.id,
        len(node_ids),
        source,
    )
    return None


@router.post("/disconnect")
async def opcua_disconnect(session: OpcUaSession = Depends(get_opcua)) -> dict[str, Any]:
    return await session.disconnect()


@router.get("/namespaces")
async def opcua_namespaces(session: OpcUaSession = Depends(get_opcua)) -> dict[str, Any]:
    try:
        return {"namespaces": await session.namespaces()}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/browse")
async def opcua_browse(
    nodeId: Optional[str] = None,
    session: OpcUaSession = Depends(get_opcua),
) -> dict[str, Any]:
    try:
        items = await session.browse(nodeId)
    except Exception as exc:
        raise _http_error(exc) from exc
    return {
        "parentNodeId": nodeId,
        "children": [
            {
                "nodeId": item.node_id,
                "browseName": item.browse_name,
                "displayName": item.display_name,
                "nodeClass": item.node_class,
            }
            for item in items
        ],
    }


@router.post("/read")
async def opcua_read(
    body: ReadBody,
    session: OpcUaSession = Depends(get_opcua),
) -> dict[str, Any]:
    if not body.nodeIds:
        raise HTTPException(status_code=400, detail="nodeIds 가 비어 있습니다.")
    try:
        return {"values": await session.read(body.nodeIds)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/write")
async def opcua_write(
    body: WriteBody,
    session: OpcUaSession = Depends(get_opcua),
) -> dict[str, Any]:
    try:
        return await session.write(body.nodeId, body.value)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/subscribe")
async def opcua_subscribe(
    body: SubscribeBody,
    session: OpcUaSession = Depends(get_opcua),
) -> dict[str, Any]:
    try:
        return await session.subscribe(body.nodeIds)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/unsubscribe")
async def opcua_unsubscribe(session: OpcUaSession = Depends(get_opcua)) -> dict[str, Any]:
    return await session.unsubscribe()


@router.get("/values")
def opcua_values(
    nodeId: Optional[str] = None,
    session: OpcUaSession = Depends(get_opcua),
) -> dict[str, Any]:
    node_ids = [nodeId] if nodeId else None
    return {"values": session.latest_values(node_ids)}


@router.get("/packages/{package_id}/nodes")
def opcua_package_nodes(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
    variablesOnly: bool = True,
) -> dict[str, Any]:
    nodes = load_engineering_nodes(repository.artifact_dir(package.id), variables_only=variablesOnly)
    if not nodes:
        raise HTTPException(
            status_code=404,
            detail="engineering.csv 가 없습니다. 산출물을 먼저 생성하세요.",
        )
    return {"packageId": package.id, "nodes": nodes}


@router.get("/packages/{package_id}/mapping")
def opcua_package_mapping(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
) -> dict[str, Any]:
    return _require_mapping(package, repository).to_dict()


@router.get("/packages/{package_id}/live")
async def opcua_package_live(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
    session: OpcUaSession = Depends(get_opcua),
) -> dict[str, Any]:
    _source, mapping, nodes, node_ids, aas_id = _package_live_source(package, repository)
    if not node_ids:
        raise HTTPException(
            status_code=404,
            detail="AAS↔OPC-UA 매핑 또는 engineering.csv 변수 노드가 없습니다.",
        )
    if session.is_connected:
        try:
            await session.ensure_subscribed(node_ids, package_id=package.id)
        except Exception as exc:
            raise _http_error(exc) from exc
    if mapping is not None:
        return mapping_live_response(mapping, session, package.id)
    return engineering_live_response(nodes, session, package.id, aas_id=aas_id)


@router.post("/packages/{package_id}/read")
async def opcua_package_read(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
    session: OpcUaSession = Depends(get_opcua),
    source: str = "mapping",
) -> dict[str, Any]:
    artifact_dir = repository.artifact_dir(package.id)
    if source == "engineering":
        return await _read_engineering_nodes(package, artifact_dir, session)
    if source != "mapping":
        raise HTTPException(status_code=400, detail="source 는 mapping 또는 engineering 이어야 합니다.")
    mapping = _require_mapping(package, repository)
    try:
        values = await session.read(mapping.required_node_ids())
    except Exception as exc:
        raise _http_error(exc) from exc
    mapped = apply_mapping(mapping, values)
    return {
        "packageId": package.id,
        "source": "mapping",
        "mappingSource": mapping.source,
        "values": mapped,
        "frame": frame_from_mapping(
            mapped,
            connected=session.is_connected,
            package_id=package.id,
            aas_id=mapping.aas_id,
        ),
    }


async def _read_engineering_nodes(
    package: AasxPackage,
    artifact_dir: Path,
    session: OpcUaSession,
) -> dict[str, Any]:
    nodes = load_engineering_nodes(artifact_dir, variables_only=True)
    if not nodes:
        raise HTTPException(
            status_code=404,
            detail="engineering.csv 변수 노드가 없습니다. 산출물을 먼저 생성하세요.",
        )
    try:
        values = await session.read([node["nodeId"] for node in nodes])
    except Exception as exc:
        raise _http_error(exc) from exc
    by_id = {item["nodeId"]: item for item in values}
    return {
        "packageId": package.id,
        "source": "engineering",
        "values": [
            {
                **node,
                **by_id.get(node["nodeId"], {}),
            }
            for node in nodes
        ],
    }
