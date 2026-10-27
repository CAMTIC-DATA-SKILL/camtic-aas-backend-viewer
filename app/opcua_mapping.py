"""AAS Property 경로와 현장 OPC-UA NodeId 를 연결한다.

산출물 `nodeset.xml` 의 가상 NodeId (`ns=1;i=…`) 가 아니라,
엣지 서버에 실제로 있는 태그를 읽기 위한 테이블이다.

찾는 순서:
1. `{artifacts}/{package_id}/mapping.json`  (현장 오버라이드)
2. `{AASX_DATA_DIR}/../mappings/{package_id}.json`  (레포 기본 매핑)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import settings
from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)

MAPPING_FILENAME = "mapping.json"

_ROBOT_STATUS = {0: "stop", 1: "run", 2: "wait"}
_DATETIME_ROLES = ("year", "month", "day", "hour", "minute", "second")


@dataclass(frozen=True)
class SourceRef:
    role: str
    node_id: str
    source_label: str = ""


@dataclass(frozen=True)
class Binding:
    id: str
    submodel_id_short: str
    path: str
    semantic_id: str = ""
    node_id: str = ""
    source_label: str = ""
    transform: str = "as_is"
    sources: tuple[SourceRef, ...] = ()

    def node_ids(self) -> list[str]:
        if self.sources:
            return [item.node_id for item in self.sources if item.node_id]
        return [self.node_id] if self.node_id else []

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "submodelIdShort": self.submodel_id_short,
            "path": self.path,
            "semanticId": self.semantic_id or None,
            "nodeId": self.node_id or None,
            "sourceLabel": self.source_label or None,
            "transform": self.transform,
        }
        if self.sources:
            payload["sources"] = [
                {
                    "role": item.role,
                    "nodeId": item.node_id,
                    "sourceLabel": item.source_label or None,
                }
                for item in self.sources
            ]
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(frozen=True)
class AasOpcUaMapping:
    package_id: str
    source: str
    aas_id: str = ""
    bindings: tuple[Binding, ...] = ()

    def required_node_ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for binding in self.bindings:
            for node_id in binding.node_ids():
                if node_id not in seen:
                    seen.add(node_id)
                    ids.append(node_id)
        return ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "packageId": self.package_id,
            "aasId": self.aas_id or None,
            "source": self.source,
            "bindingCount": len(self.bindings),
            "bindings": [binding.to_dict() for binding in self.bindings],
        }


def mapping_candidates(package_id: str, artifact_dir: Path) -> list[Path]:
    return [
        artifact_dir / MAPPING_FILENAME,
        settings.aasx_data_dir.parent / "mappings" / f"{package_id}.json",
    ]


def load_mapping(package_id: str, artifact_dir: Path) -> Optional[AasOpcUaMapping]:
    for path in mapping_candidates(package_id, artifact_dir):
        if path.is_file():
            return _read_mapping(path, package_id)
    return None


def apply_mapping(
    mapping: AasOpcUaMapping, live_values: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {item["nodeId"]: item for item in live_values if item.get("nodeId")}
    return [_apply_binding(binding, by_id) for binding in mapping.bindings]


def split_engineering_path(aas_path: str, package_id: str = "") -> tuple[str, str]:
    """`/{package}/{shell}/{submodel}/{a.b}` → (submodelIdShort, dotted path)."""
    parts = [part for part in (aas_path or "").split("/") if part]
    if package_id and parts and parts[0] == package_id:
        parts = parts[1:]
    if len(parts) >= 2:
        parts = parts[1:]
    if not parts:
        return "", ""
    return parts[0], ".".join(parts[1:])


def apply_engineering(
    nodes: list[dict[str, Any]],
    live_values: list[dict[str, Any]],
    package_id: str = "",
) -> list[dict[str, Any]]:
    by_id = {item["nodeId"]: item for item in live_values if item.get("nodeId")}
    mapped: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("nodeId") or "")
        if not node_id:
            continue
        submodel, path = split_engineering_path(str(node.get("aasPath") or ""), package_id)
        live = by_id.get(node_id, {})
        raw_value = live.get("value")
        usable = _usable(live) and raw_value is not None
        mapped.append(
            {
                "id": str(node.get("browseName") or path or node_id),
                "submodelIdShort": submodel,
                "path": path,
                "semanticId": node.get("semanticId") or None,
                "nodeId": node_id,
                "sourceLabel": node.get("displayName") or None,
                "transform": "as_is",
                "value": raw_value,
                "rawValue": raw_value,
                "status": live.get("status") or "Bad",
                "isGood": bool(live.get("isGood")),
                "usable": usable,
                "sourceTimestamp": live.get("sourceTimestamp"),
                "serverTimestamp": live.get("serverTimestamp"),
            }
        )
    return mapped


def compose_datetime(parts: dict[str, Any]) -> Optional[str]:
    try:
        return datetime(
            int(parts["year"]),
            int(parts["month"]),
            int(parts["day"]),
            int(parts.get("hour") or 0),
            int(parts.get("minute") or 0),
            int(parts.get("second") or 0),
        ).isoformat()
    except (TypeError, ValueError, KeyError):
        return None


def as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "run"}:
        return True
    if text in {"0", "false", "no", "off", "stop"}:
        return False
    return None


def robot_status(value: Any) -> Optional[str]:
    try:
        return _ROBOT_STATUS[int(value)]
    except (TypeError, ValueError, KeyError):
        return None if value is None else str(value)


def _read_mapping(path: Path, package_id: str) -> AasOpcUaMapping:
    raw = json.loads(path.read_text(encoding="utf-8"))
    bindings = tuple(_parse_binding(item, index) for index, item in enumerate(raw.get("bindings") or []))
    if not bindings:
        raise ValueError(f"매핑 파일이 비어 있습니다: {path}")
    return AasOpcUaMapping(
        package_id=str(raw.get("packageId") or package_id),
        source=str(path),
        aas_id=str(raw.get("aasId") or ""),
        bindings=bindings,
    )


def _parse_binding(raw: dict[str, Any], index: int) -> Binding:
    sources = tuple(
        SourceRef(
            role=str(item.get("role") or "").strip(),
            node_id=str(item.get("nodeId") or "").strip(),
            source_label=str(item.get("sourceLabel") or "").strip(),
        )
        for item in (raw.get("sources") or [])
        if item.get("nodeId")
    )
    path = str(raw.get("path") or "").strip()
    submodel = str(raw.get("submodelIdShort") or "").strip()
    if not path or not submodel:
        raise ValueError(f"bindings[{index}] 에 submodelIdShort/path 가 필요합니다.")
    return Binding(
        id=str(raw.get("id") or f"binding-{index + 1}"),
        submodel_id_short=submodel,
        path=path,
        semantic_id=str(raw.get("semanticId") or "").strip(),
        node_id=str(raw.get("nodeId") or "").strip(),
        source_label=str(raw.get("sourceLabel") or "").strip(),
        transform=str(raw.get("transform") or "as_is").strip() or "as_is",
        sources=sources,
    )


def _usable(live: dict[str, Any]) -> bool:
    if live.get("isGood"):
        return True
    return str(live.get("status") or "").startswith("Uncertain")


def _apply_binding(binding: Binding, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = binding.to_dict()
    payload["live"] = True
    if binding.transform == "compose_datetime":
        return _apply_compose(binding, by_id, payload)

    live = by_id.get(binding.node_id, {})
    raw_value = live.get("value")
    value = _transform(binding.transform, raw_value)
    usable = _usable(live) and value is not None
    payload.update(
        {
            "value": value,
            "rawValue": raw_value,
            "status": live.get("status") or "Bad",
            "isGood": bool(live.get("isGood")),
            "usable": usable,
            "sourceTimestamp": live.get("sourceTimestamp"),
            "serverTimestamp": live.get("serverTimestamp"),
        }
    )
    return payload


def _apply_compose(
    binding: Binding, by_id: dict[str, dict[str, Any]], payload: dict[str, Any]
) -> dict[str, Any]:
    parts: dict[str, Any] = {}
    sources = []
    good = True
    usable = True
    latest_source = None
    latest_server = None
    for item in binding.sources:
        live = by_id.get(item.node_id, {})
        value = live.get("value")
        if item.role in _DATETIME_ROLES:
            parts[item.role] = value
        sources.append(
            {
                "role": item.role,
                "nodeId": item.node_id,
                "sourceLabel": item.source_label or None,
                "value": value,
                "status": live.get("status") or "Bad",
                "isGood": bool(live.get("isGood")),
            }
        )
        good = good and bool(live.get("isGood"))
        usable = usable and _usable(live)
        latest_source = live.get("sourceTimestamp") or latest_source
        latest_server = live.get("serverTimestamp") or latest_server
    composed = compose_datetime(parts)
    if good and composed is not None:
        status = "Good"
    elif usable and composed is not None:
        status = "UncertainLastUsableValue"
    else:
        status = "Bad"
    payload.update(
        {
            "value": composed,
            "status": status,
            "isGood": bool(good and composed is not None),
            "usable": bool(usable and composed is not None),
            "sourceTimestamp": latest_source,
            "serverTimestamp": latest_server,
            "sources": sources,
        }
    )
    return payload


def _transform(name: str, value: Any) -> Any:
    if name == "as_bool":
        return as_bool(value)
    if name == "robot_status":
        return robot_status(value)
    if name in {"as_is", "compose_datetime"}:
        return value
    logger.warning("알 수 없는 매핑 transform: %s", name)
    return value
