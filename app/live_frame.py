"""AAS Property 라이브 값을 실시간 그래프 프레임으로 정규화한다."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

JOINT_RE = re.compile(r"(?:^|[.\-_])J([1-6])(?:[.\-_]|$)", re.I)
CART_NAMES = {
    "x": "X",
    "y": "Y",
    "z": "Z",
    "rx": "Rx",
    "ry": "Ry",
    "rz": "Rz",
    "component1": "Rx",
    "component2": "Ry",
    "component3": "Rz",
}


def series_key(submodel: str, path: str, ident: str = "") -> str:
    if submodel and path:
        return f"{submodel}/{path}"
    return ident or path or submodel


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return float(value)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        named = {"stop": 0.0, "run": 1.0, "wait": 2.0, "true": 1.0, "false": 0.0}
        if text in named:
            return named[text]
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _leaf(path: str, ident: str) -> str:
    if path:
        return path.rsplit(".", 1)[-1]
    return ident


def _group_label(submodel: str, path: str) -> str:
    parents = [part for part in path.split(".")[:-1] if part]
    parts = [part for part in (submodel, *parents) if part]
    return " · ".join(parts) or submodel or "AAS"


def _is_timestamp(path: str, ident: str, transform: str) -> bool:
    if transform == "compose_datetime":
        return True
    leaf = _leaf(path, ident).lower()
    return leaf in {"timestamp", "time", "datetime"} or ident.lower() in {"timestamp", "time"}


def _set_nested(root: dict[str, Any], parts: list[str], value: Any) -> None:
    cursor = root
    for part in parts[:-1]:
        current = cursor.get(part)
        if not isinstance(current, dict):
            current = {}
            cursor[part] = current
        cursor = current
    cursor[parts[-1]] = value


def _fill_joint(joint: dict[str, dict[str, Any]], path: str, ident: str, value: float) -> None:
    match = JOINT_RE.search(path) or JOINT_RE.search(ident)
    if not match:
        return
    name = f"J{match.group(1)}"
    bucket = joint.setdefault(name, {})
    text = f"{path} {ident}".lower()
    if "vel" in text or "speed" in text:
        bucket["Velocity"] = value
    else:
        bucket["Position"] = value


def _fill_cartesian(cart: dict[str, Any], path: str, ident: str, value: float) -> None:
    leaf = _leaf(path, ident).lower()
    key = CART_NAMES.get(leaf)
    if key:
        cart[key] = value


def frame_from_mapping(
    values: list[dict[str, Any]],
    *,
    connected: bool,
    package_id: str = "",
    aas_id: str = "",
) -> dict[str, Any]:
    series: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    joint: dict[str, dict[str, Any]] = {}
    cart: dict[str, Any] = {}
    timestamp = None
    error = ""

    for item in values or []:
        submodel = str(item.get("submodelIdShort") or "")
        path = str(item.get("path") or "")
        ident = str(item.get("id") or "")
        transform = str(item.get("transform") or "")
        value = item.get("value")
        raw = item.get("rawValue", value)
        parts = [part for part in (submodel, *path.split(".")) if part]
        if parts:
            _set_nested(snapshot, parts, value)
        if _is_timestamp(path, ident, transform):
            timestamp = value
            continue
        if not item.get("usable") and not item.get("isGood"):
            if not error:
                error = str(item.get("status") or "Bad")
            if _as_number(raw if transform in {"robot_status", "as_bool"} else value) is None:
                continue

        numeric = _as_number(raw if transform in {"robot_status", "as_bool"} else value)
        if numeric is None:
            continue
        key = series_key(submodel, path, ident)
        series.append(
            {
                "id": key,
                "submodelIdShort": submodel or None,
                "path": path or None,
                "label": _leaf(path, ident) or key,
                "group": _group_label(submodel, path),
                "semanticId": item.get("semanticId"),
                "value": numeric,
                "displayValue": value,
                "nodeId": item.get("nodeId"),
            }
        )
        _fill_joint(joint, path, ident, numeric)
        _fill_cartesian(cart, path, ident, numeric)

    return {
        "packageId": package_id,
        "aasId": aas_id or None,
        "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
        "seq": None,
        "online": bool(connected),
        "snapshot": snapshot,
        "series": series,
        "joint": joint or None,
        "cartesian": cart or None,
        "error": error,
    }
