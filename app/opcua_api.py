from pathlib import Path
from typing import Any, Optional

from asyncua import ua
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from config import settings
from utils.log_setup import LogSetup

from .live_frame import frame_from_mapping
from .opc_ua import OpcUaClientError, OpcUaSession
from .opcua_catalog import CatalogServer, load_catalog
from .opcua_export import load_engineering_nodes
from .opcua_mapping import (
    AasOpcUaMapping,
    apply_engineering,
    apply_mapping,
    load_mapping,
    rewrite_node_id,
    split_engineering_path,
)
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
    replace: bool = True


def get_repository(request: Request) -> AasxRepository:
    return request.app.state.repository


def get_opcua(request: Request) -> OpcUaSession:
    return request.app.state.opcua


def get_package(package_id: str, repository: AasxRepository = Depends(get_repository)) -> AasxPackage:
    package = repository.get(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"Package '{package_id}' not found")
    return package


def owns_live_subscription(session: OpcUaSession, package_id: str) -> bool:
    """LIVE 캐시는 이 연결에 등록된 AASX 들이 함께 읽는다."""
    return package_id in session.subscribe_package_ids


def _live_mismatch_message(session: OpcUaSession) -> str:
    subscribed = session.subscribe_package_ids
    if not subscribed:
        return "구독 중인 AAS 패키지가 없습니다."
    names = ", ".join(subscribed)
    return f"LIVE는 {names} 구독입니다. 이 패키지 값은 표시하지 않습니다."


def empty_live_response(
    session: OpcUaSession,
    package_id: str,
    *,
    aas_id: str = "",
    source: str = "engineering",
) -> dict[str, Any]:
    return {
        "packageId": package_id,
        "source": source,
        "subscribeMatch": False,
        "message": _live_mismatch_message(session),
        "catalogServerId": session.catalog_server_id,
        "subscribePackageId": session.subscribe_package_id,
        "subscribePackageIds": session.subscribe_package_ids,
        "subscribedNodeIds": session.subscribed_node_ids,
        "latestCount": len(session.latest_values()),
        "values": [],
        "frame": frame_from_mapping(
            [],
            connected=False,
            package_id=package_id,
            aas_id=aas_id,
        ),
    }


def captured_property_values(
    package: AasxPackage,
    repository: AasxRepository,
    session: OpcUaSession,
) -> list[dict[str, Any]]:
    """뷰어에 올라가는 것과 같은 구독 값. 이 패키지가 구독 중이 아니거나 값이 없으면 빈 목록."""
    if package.error:
        return []
    field_namespace = _field_namespace(session)
    _source, nodes, _node_ids, aas_id = _package_live_source(
        package, repository, field_namespace=field_namespace
    )
    if not nodes:
        return []
    payload = engineering_live_response(
        nodes,
        session,
        package.id,
        aas_id=aas_id,
        field_namespace=field_namespace,
    )
    captured: list[dict[str, Any]] = []
    for item in payload.get("values") or []:
        if not item.get("usable") or item.get("value") is None:
            continue
        submodel = str(item.get("submodelIdShort") or "")
        path = str(item.get("path") or "")
        if not submodel or not path:
            continue
        captured.append({"submodelIdShort": submodel, "path": path, "value": item["value"]})
    return captured


def mapping_live_response(
    mapping: AasOpcUaMapping,
    session: OpcUaSession,
    package_id: str,
) -> dict[str, Any]:
    if not owns_live_subscription(session, package_id):
        payload = empty_live_response(session, package_id, aas_id=mapping.aas_id, source="subscription")
        payload["mappingSource"] = mapping.source
        return payload
    mapped = apply_mapping(mapping, session.latest_values())
    return {
        "packageId": package_id,
        "source": "subscription",
        "subscribeMatch": True,
        "mappingSource": mapping.source,
        "catalogServerId": session.catalog_server_id,
        "subscribePackageId": session.subscribe_package_id,
        "subscribePackageIds": session.subscribe_package_ids,
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
    field_namespace: Optional[int] = None,
) -> dict[str, Any]:
    if not owns_live_subscription(session, package_id):
        return empty_live_response(session, package_id, aas_id=aas_id)
    mapped = apply_engineering(
        nodes,
        session.latest_values(),
        package_id=package_id,
        field_namespace=field_namespace,
    )
    mapped = [item for item in mapped if item.get("usable") or item.get("isGood")]
    return {
        "packageId": package_id,
        "source": "engineering",
        "subscribeMatch": True,
        "catalogServerId": session.catalog_server_id,
        "subscribePackageId": session.subscribe_package_id,
        "subscribePackageIds": session.subscribe_package_ids,
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
    mapping = load_mapping(package.id, repository.package_dir(package.id))
    if mapping is None:
        mapping_path = settings.storage.mappings_dir / f"{package.id}.json"
        raise HTTPException(
            status_code=404,
            detail=(
                "AAS↔OPC-UA 매핑이 없습니다. "
                f"패키지 {settings.storage.package_config_dir}/mapping.json 또는 {mapping_path} 에 두세요."
            ),
        )
    return mapping


_LIVE_SUBMODELS = {"RobotKinematicState", "RobotOperationalState"}


def _field_namespace(session: OpcUaSession) -> Optional[int]:
    server_id = session.catalog_server_id
    if not server_id:
        return None
    server = load_catalog().get(server_id)
    return server.node_id_namespace if server else None


def _engineering_live_nodes(nodes: list[dict[str, Any]], package_id: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for node in nodes:
        submodel, _path = split_engineering_path(str(node.get("aasPath") or ""), package_id)
        if submodel in _LIVE_SUBMODELS:
            selected.append(node)
    return selected


def _shell_name(aas_path: str, package_id: str) -> str:
    parts = [part for part in (aas_path or "").split("/") if part]
    if package_id and parts and parts[0] == package_id:
        parts = parts[1:]
    return parts[0] if parts else ""


def _tag_node_id(address: str) -> str:
    text = address.strip()
    if text.startswith("ns="):
        return text
    return f"ns=1;s={text}"


def _tag_live_nodes(nodes: list[dict[str, Any]], package_id: str) -> list[dict[str, Any]]:
    """IOInterfaceMapping 의 PLC 주소를 OPC-UA 태그로, AASPath 를 값이 붙을 속성으로 쓴다.

    캐시 키는 태그 주소 하나다. 같은 D2010 을 여러 파일이 가리켜도 구독과 저장은 한 번이다.
    """
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for node in nodes:
        submodel, path = split_engineering_path(str(node.get("aasPath") or ""), package_id)
        if submodel != "IOInterfaceMapping" or "." not in path:
            continue
        group, leaf = path.rsplit(".", 1)
        if leaf not in {"PLCAddress", "AASPath"}:
            continue
        grouped.setdefault(group, {})[leaf] = node

    selected: list[dict[str, Any]] = []
    for fields in grouped.values():
        address = str((fields.get("PLCAddress") or {}).get("templateValue") or "").strip()
        target = str((fields.get("AASPath") or {}).get("templateValue") or "").strip().strip("/")
        sample = fields.get("PLCAddress") or {}
        shell = _shell_name(str(sample.get("aasPath") or ""), package_id)
        if not address or not target or not shell:
            continue
        selected.append(
            {
                "nodeId": _tag_node_id(address),
                "browseName": target.rsplit("/", 1)[-1],
                "displayName": address,
                "aasPath": f"/{package_id}/{shell}/{target}",
            }
        )
    return selected


def _package_live_source(
    package: AasxPackage,
    repository: AasxRepository,
    field_namespace: Optional[int] = None,
) -> tuple[str, list[dict[str, Any]], list[str], str]:
    aas_id = str(package.shells[0].id) if package.shells else ""
    records = load_engineering_nodes(repository.artifact_dir(package.id), variables_only=True)
    nodes = [
        *_engineering_live_nodes(records, package.id),
        *_tag_live_nodes(records, package.id),
    ]
    node_ids = list(
        dict.fromkeys(
            rewrite_node_id(str(node["nodeId"]), field_namespace)
            for node in nodes
            if node.get("nodeId")
        )
    )
    return "engineering", nodes, node_ids, aas_id


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
    resolved_ids = [item.strip() for item in (server.aas_ids if server else ()) if item.strip()]
    if aas_id and aas_id.strip():
        resolved_ids = [aas_id.strip()]
    subscribe_error = await subscribe_engineering(
        session,
        repository,
        aas_ids=resolved_ids,
        package_id=(package_id or "").strip(),
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
        ",".join(resolved_ids) or "-",
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
    aas_ids: Optional[list[str]] = None,
    package_id: str = "",
) -> Optional[str]:
    """한 OPC-UA 연결에 여러 AASX 를 등록한다. 같은 태그 주소는 한 번만 구독한다."""
    requested = [item.strip() for item in (aas_ids or []) if item and item.strip()]
    if aas_id.strip() and aas_id.strip() not in requested:
        requested.append(aas_id.strip())

    packages: list[AasxPackage] = []
    missing: list[str] = []
    if requested:
        for item in requested:
            package = repository.find_by_shell_id(item)
            if package is None:
                missing.append(item)
            elif all(package.id != current.id for current in packages):
                packages.append(package)
    elif package_id:
        package = repository.get(package_id)
        if package is None:
            return f"Package '{package_id}' not found"
        packages.append(package)
    if not packages:
        if missing:
            return f"AAS id '{missing[0]}' 에 해당하는 패키지가 없습니다."
        logger.info("OPC-UA 토픽 등록 생략: aasId/packageId 가 없습니다.")
        return None

    field_namespace = _field_namespace(session)
    node_ids: list[str] = []
    package_ids: list[str] = []
    for package in packages:
        _source, _nodes, ids, _aas_id = _package_live_source(
            package, repository, field_namespace=field_namespace
        )
        if not ids:
            logger.info("OPC-UA 라이브 구독 항목 없음: package=%s", package.id)
            continue
        package_ids.append(package.id)
        node_ids.extend(ids)
    node_ids = list(dict.fromkeys(node_ids))
    if not node_ids:
        detail = f"AAS id '{missing[0]}' 에 해당하는 패키지가 없습니다." if missing else None
        return detail

    try:
        await session.subscribe(node_ids, package_ids=package_ids)
    except Exception as exc:
        logger.warning(
            "OPC-UA 토픽 등록 실패: packages=%s (%s)",
            package_ids,
            exc,
        )
        return str(exc)
    logger.info(
        "OPC-UA 토픽 등록: packages=%s count=%d uniqueTags=%d",
        package_ids,
        len(node_ids),
        len(node_ids),
    )
    if missing:
        return f"AAS id '{missing[0]}' 에 해당하는 패키지가 없습니다."
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


def _browse_item_dict(item) -> dict[str, Any]:
    return {
        "nodeId": item.node_id,
        "browseName": item.browse_name,
        "displayName": item.display_name,
        "nodeClass": item.node_class,
    }


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
        "children": [_browse_item_dict(item) for item in items],
    }


@router.get("/variables")
async def opcua_variables(
    nodeId: Optional[str] = None,
    session: OpcUaSession = Depends(get_opcua),
) -> dict[str, Any]:
    try:
        items, truncated = await session.browse_variables(nodeId)
    except Exception as exc:
        raise _http_error(exc) from exc
    return {
        "parentNodeId": nodeId,
        "truncated": truncated,
        "variables": [_browse_item_dict(item) for item in items],
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
        node_ids = list(dict.fromkeys(body.nodeIds))
        if not body.replace:
            node_ids = list(dict.fromkeys([*session.subscribed_node_ids, *node_ids]))
        return await session.subscribe(node_ids)
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
def opcua_package_live(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
    session: OpcUaSession = Depends(get_opcua),
) -> dict[str, Any]:
    """구독 캐시만 읽는다. 웹 뷰어는 이 API로 현장 구독을 걸거나 바꾸지 않는다.

    요청 패키지가 ``subscribePackageId`` 와 다를 때는 캐시를 붙이지 않는다.
    """
    field_namespace = _field_namespace(session)
    _source, nodes, _node_ids, aas_id = _package_live_source(
        package, repository, field_namespace=field_namespace
    )
    return engineering_live_response(
        nodes,
        session,
        package.id,
        aas_id=aas_id,
        field_namespace=field_namespace,
    )


@router.post("/packages/{package_id}/read")
async def opcua_package_read(
    package: AasxPackage = Depends(get_package),
    repository: AasxRepository = Depends(get_repository),
    session: OpcUaSession = Depends(get_opcua),
    source: str = "auto",
) -> dict[str, Any]:
    artifact_dir = repository.artifact_dir(package.id)
    if source == "auto":
        source = "mapping" if load_mapping(package.id, repository.package_dir(package.id)) else "engineering"
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
    field_namespace = _field_namespace(session)
    field_ids = [rewrite_node_id(node["nodeId"], field_namespace) for node in nodes]
    try:
        values = await session.read(field_ids)
    except Exception as exc:
        raise _http_error(exc) from exc
    by_id = {item["nodeId"]: item for item in values}
    return {
        "packageId": package.id,
        "source": "engineering",
        "values": [
            {
                **node,
                **by_id.get(rewrite_node_id(node["nodeId"], field_namespace), {}),
                "nodeId": rewrite_node_id(node["nodeId"], field_namespace),
            }
            for node in nodes
        ],
    }
