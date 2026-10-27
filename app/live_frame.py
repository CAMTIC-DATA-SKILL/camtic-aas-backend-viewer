"""AAS Property 라이브 값을 실시간 그래프 프레임으로 정규화한다."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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


def frame_from_collector(payload: dict[str, Any]) -> dict[str, Any]:
    meta = payload.get("Meta") or {}
    joint = payload.get("Joint") or {}
    cart = payload.get("Cartesian") or {}
    position = cart.get("Position") or {}
    orientation = cart.get("Orientation") or {}
    cartesian = {**position, **orientation} if position or orientation else None
    series: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {
        "Meta": meta,
        "Status": payload.get("Status") or {},
        "Joint": joint,
        "Cartesian": cart,
        "Errors": payload.get("Errors") or {},
    }
    for name, item in joint.items():
        if not isinstance(item, dict):
            continue
        if item.get("Position") is not None:
            series.append(
                {
                    "id": f"Joint/{name}.Position",
                    "submodelIdShort": "Joint",
                    "path": f"{name}.Position",
                    "label": f"{name} Position",
                    "group": "Joint",
                    "value": item.get("Position"),
                    "displayValue": item.get("Position"),
                }
            )
        if item.get("Velocity") is not None:
            series.append(
                {
                    "id": f"Joint/{name}.Velocity",
                    "submodelIdShort": "Joint",
                    "path": f"{name}.Velocity",
                    "label": f"{name} Velocity",
                    "group": "Joint",
                    "value": item.get("Velocity"),
                    "displayValue": item.get("Velocity"),
                }
            )
    if cartesian:
        for key in ("X", "Y", "Z", "Rx", "Ry", "Rz"):
            if cartesian.get(key) is None:
                continue
            series.append(
                {
                    "id": f"Cartesian/{key}",
                    "submodelIdShort": "Cartesian",
                    "path": key,
                    "label": key,
                    "group": "Cartesian",
                    "value": cartesian[key],
                    "displayValue": cartesian[key],
                }
            )
    errors = payload.get("Errors") or {}
    return {
        "packageId": None,
        "aasId": None,
        "timestamp": meta.get("Timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "seq": meta.get("Seq"),
        "online": bool(meta.get("Online")),
        "snapshot": snapshot,
        "series": series,
        "joint": joint or None,
        "cartesian": cartesian,
        "error": errors.get("LastError") or "",
        "raw": payload,
    }


def flatten_frame(frame: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "Timestamp": frame.get("timestamp"),
        "PackageId": frame.get("packageId"),
        "AasId": frame.get("aasId"),
        "Online": int(bool(frame.get("online"))),
    }
    for item in frame.get("series") or []:
        ident = item.get("id")
        if ident:
            row[str(ident)] = item.get("value")
    row["LastError"] = frame.get("error") or ""
    return row


def validate_collector_url(url: str) -> str:
    text = (url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("수집기 URL 은 http(s)://host[:port] 형식이어야 합니다.")
    if parsed.username or parsed.password:
        raise ValueError("수집기 URL 에 사용자 정보를 넣을 수 없습니다.")
    return text.rstrip("/")


def collector_data_url(url: str) -> str:
    base = validate_collector_url(url)
    parsed = urlparse(base)
    if parsed.path.rstrip("/").endswith("/api/data"):
        return base
    return f"{base}/api/data"


def http_get_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 503:
            raise ValueError("수집기에 아직 데이터가 없습니다.") from exc
        raise ValueError(f"수집기 응답 {exc.code}: {body or exc.reason}") from exc
    except URLError as exc:
        raise ValueError(f"수집기에 연결할 수 없습니다: {exc.reason}") from exc
