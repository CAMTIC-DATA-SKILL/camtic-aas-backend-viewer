"""엣지 값을 MQTT로 옮길 때의 토픽과 JSON.

토픽은 ``engineering.csv`` 의 AasPath 이다. 예:
``aas/Hyundai_HH7_AAS_Template_v0.3/HH7/RobotKinematicState/JointPosition/J1``

발행 프로세스와 AAS 서버가 이 형식을 같이 쓴다. 둘은 서로의 주소를 모른다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils.log_setup import LogSetup

from .opcua_export import load_engineering_nodes
from .opcua_mapping import rewrite_node_id, split_engineering_path

logger = LogSetup.get_logger(__name__)

_STATUS_CODE_MAX = 2**32 - 1


@dataclass(frozen=True)
class PublishPoint:
    package_id: str
    node_id: str
    topic: str
    path: str


@dataclass(frozen=True)
class FieldUpdate:
    package_id: str
    path: str
    node_id: str
    value: Any
    status: str
    source_timestamp: Optional[datetime]
    status_code: int


def topic_from_aas_path(aas_path: str, prefix: str) -> str:
    """AasPath 를 브로커 토픽으로 만든다. 앞뒤 슬래시만 정리한다."""
    body = "/".join(part for part in (aas_path or "").split("/") if part)
    if not body:
        raise ValueError("AasPath 가 비어 있습니다.")
    if any("+" in part or "#" in part for part in body.split("/")):
        raise ValueError(f"MQTT 토픽에 와일드카드(+, #)가 있습니다: {aas_path}")
    root = prefix.strip().strip("/")
    if not root:
        raise ValueError("MQTT 토픽 접두어가 비어 있습니다.")
    return f"{root}/{body}"


def message_path(aas_path: str, package_id: str) -> str:
    """``RobotKinematicState/JointPosition/J1`` 처럼 서브모델부터의 경로."""
    submodel, dotted = split_engineering_path(aas_path, package_id)
    if not submodel:
        return ""
    if not dotted:
        return submodel
    return f"{submodel}/{dotted.replace('.', '/')}"


_WILDCARD_DEPTH = 4
_WILDCARD_BREADTH = 40


def validate_wildcard(pattern: str) -> str:
    """MQTT 필터면 정리된 문자열을 돌려준다. 빈 문자열은 기본 조회다."""
    raw = (pattern or "").strip()
    if not raw:
        return ""
    if len(raw) > 500:
        raise ValueError("와일드카드가 너무 깁니다.")
    parts = raw.split("/")
    if any(part == "" for part in parts):
        raise ValueError("와일드카드 경로에 빈 단계가 있습니다.")
    if any(part != "#" and "#" in part for part in parts) or "#" in parts[:-1]:
        raise ValueError("# 는 마지막 단계에만 쓸 수 있습니다.")
    if any(part != "+" and "+" in part for part in parts):
        raise ValueError("+ 는 한 단계를 통째로만 대체합니다.")
    return raw


def resolve_wildcard(pattern: str, prefix: str) -> str:
    """빈 값과 구독 전체(``prefix/#``)는 기본 조회다. 나머지는 전체 토픽 필터로 맞춘다."""
    raw = validate_wildcard(pattern)
    if not raw:
        return ""
    root = prefix.strip().strip("/")
    if raw in {"#", root, f"{root}/#"}:
        return ""
    if root and (raw == root or raw.startswith(f"{root}/")):
        return raw
    if not root:
        return raw
    return f"{root}/{raw.lstrip('/')}"


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT 구독 필터. ``+`` 는 한 단계, ``#`` 는 마지막에서 0단계 이상을 포함한다."""
    return _match_parts(pattern.split("/"), topic.split("/"))


def branch_choices(prefix: str, topics: list[str]) -> list[dict[str, Any]]:
    """패키지와 바로 아래 한 단계만 돌려준다.

    더 깊은 경로는 선택 목록에 넣지 않는다. 그 아래는 와일드카드로 적는다.
    """
    root = [part for part in prefix.strip().strip("/").split("/") if part]
    package_topics: dict[str, int] = {}
    children: dict[str, dict[str, int]] = {}
    for topic in topics:
        parts = [part for part in topic.split("/") if part]
        if root and parts[: len(root)] != root:
            continue
        relative = parts[len(root) :]
        if not relative:
            continue
        package = relative[0]
        package_topics[package] = package_topics.get(package, 0) + 1
        if len(relative) < 2:
            continue
        child = children.setdefault(package, {})
        name = relative[1]
        child[name] = child.get(name, 0) + 1
    found: list[dict[str, Any]] = []
    for package in sorted(package_topics):
        counts = children.get(package, {})
        found.append(
            {
                "package": package,
                "topics": package_topics[package],
                "children": [
                    {"name": name, "topics": counts[name]}
                    for name in sorted(counts)
                ],
            }
        )
    return found


def suggested_wildcards(prefix: str, topics: list[str]) -> list[dict[str, Any]]:
    """받아 둔 토픽에서 패키지와 그 아래 경로의 ``#`` 필터를 만든다.

    한 단계의 가지가 너무 많으면 그 아래는 펼치지 않는다.
    """
    root = [part for part in prefix.strip().strip("/").split("/") if part]
    tree: dict[str, dict[str, Any]] = {}
    for topic in topics:
        parts = [part for part in topic.split("/") if part]
        if root and parts[: len(root)] != root:
            continue
        node = tree
        for part in parts[len(root) : _WILDCARD_DEPTH + len(root) + 1]:
            node = node.setdefault(part, {})
    found: list[dict[str, Any]] = []

    def walk(node: dict[str, dict[str, Any]], path: list[str]) -> None:
        if len(path) >= _WILDCARD_DEPTH or len(node) > _WILDCARD_BREADTH:
            return
        for name in sorted(node):
            child = node[name]
            if not child:
                continue
            next_path = path + [name]
            pattern = "/".join(next_path) + "/#"
            found.append({"pattern": pattern, "label": pattern, "depth": len(next_path)})
            walk(child, next_path)

    walk(tree, [])
    found.sort(key=lambda item: (item["depth"], item["label"]))
    return found


def _match_parts(pattern: list[str], topic: list[str]) -> bool:
    for index, part in enumerate(pattern):
        if part == "#":
            return index == len(pattern) - 1
        if index >= len(topic):
            return False
        if part != "+" and part != topic[index]:
            return False
    return len(topic) == len(pattern)


def format_timestamp(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(raw: Any) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"sourceTimestamp 형식이 아닙니다: {raw!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def value_signature(value: Any, status: str) -> str:
    return json.dumps(
        {"s": status, "v": json_safe(value)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def encode_payload(
    *,
    package_id: str,
    path: str,
    node_id: str,
    value: Any,
    status: str,
    source_timestamp: Optional[datetime],
    status_code: int,
) -> str:
    body = {
        "packageId": package_id,
        "path": path,
        "nodeId": node_id,
        "value": json_safe(value),
        "status": status or "Good",
        "sourceTimestamp": format_timestamp(source_timestamp),
        "statusCode": int(status_code),
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def decode_payload(payload: bytes | str) -> Optional[FieldUpdate]:
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    package_id = str(raw.get("packageId") or "").strip()
    path = str(raw.get("path") or "").strip()
    node_id = str(raw.get("nodeId") or "").strip()
    if not package_id or not path or not node_id:
        return None
    status = str(raw.get("status") or "Good").strip() or "Good"
    status_code = _status_code(raw.get("statusCode"), status)
    if status_code is None:
        return None
    try:
        source_timestamp = parse_timestamp(raw.get("sourceTimestamp"))
    except ValueError:
        return None
    return FieldUpdate(
        package_id=package_id,
        path=path,
        node_id=node_id,
        value=raw.get("value"),
        status=status,
        source_timestamp=source_timestamp,
        status_code=status_code,
    )


def load_publish_plan(
    packages_dir: Path,
    opcua_dirname: str,
    namespace: Optional[int],
    prefix: str,
    aas_ids: tuple[str, ...] = (),
) -> list[PublishPoint]:
    """패키지 ``engineering.csv`` 의 Variable 만 발행 목록으로 만든다.

    카탈로그 ``aasIds`` 가 있으면 그 AAS 가 들어 있는 패키지만 포함한다.
    """
    if not packages_dir.is_dir():
        return []
    allowed = {item.strip() for item in aas_ids if item and item.strip()}
    points: list[PublishPoint] = []
    seen_topics: set[str] = set()
    for package_dir in sorted(path for path in packages_dir.iterdir() if path.is_dir()):
        nodes = load_engineering_nodes(package_dir / opcua_dirname, variables_only=False)
        if not nodes:
            continue
        if allowed and not any(node.get("aasId") in allowed for node in nodes):
            continue
        for node in nodes:
            if node.get("nodeClass") != "Variable":
                continue
            aas_path = str(node.get("aasPath") or "")
            node_id = str(node.get("nodeId") or "")
            if not aas_path or not node_id:
                continue
            try:
                topic = topic_from_aas_path(aas_path, prefix)
            except ValueError as exc:
                logger.warning("발행 제외: %s", exc)
                continue
            if topic in seen_topics:
                logger.warning("토픽이 겹쳐 발행 제외: %s", topic)
                continue
            path = message_path(aas_path, package_dir.name)
            if not path:
                continue
            seen_topics.add(topic)
            points.append(
                PublishPoint(
                    package_id=package_dir.name,
                    node_id=rewrite_node_id(node_id, namespace),
                    topic=topic,
                    path=path,
                )
            )
    return points


def index_by_node(points: list[PublishPoint]) -> dict[str, list[PublishPoint]]:
    grouped: dict[str, list[PublishPoint]] = {}
    for point in points:
        grouped.setdefault(point.node_id, []).append(point)
    return grouped


def _status_code(raw: Any, status: str) -> Optional[int]:
    if raw is None or raw == "":
        return _default_status_code(status)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > _STATUS_CODE_MAX:
        return None
    return value


def _default_status_code(status: str) -> int:
    """statusCode 가 없을 때 OPC-UA 품질 비트만 맞춘다. Good=0, Uncertain, Bad."""
    if status.startswith("Good"):
        return 0
    if status.startswith("Uncertain"):
        return 0x40000000
    return 0x80000000
