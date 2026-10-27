from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from asyncua import ua


def serialize_value(value: Any) -> Any:
    """OPC-UA Variant 값을 JSON 으로 내릴 수 있게 변환한다."""
    if value is None:
        return None
    if isinstance(value, ua.Variant):
        return serialize_value(value.Value)
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "hex", "bytes": value.hex(), "length": len(value)}
    if isinstance(value, (list, tuple)):
        return [serialize_value(item) for item in value]
    if isinstance(value, ua.LocalizedText):
        return {"text": value.Text, "locale": value.Locale}
    if isinstance(value, ua.QualifiedName):
        return value.to_string()
    if isinstance(value, (ua.NodeId, ua.ExpandedNodeId)):
        return value.to_string()
    if hasattr(value, "to_string"):
        try:
            return value.to_string()
        except Exception:
            pass
    return str(value)


def serialize_data_value(node_id: str, data_value: ua.DataValue) -> dict[str, Any]:
    status = data_value.StatusCode
    return {
        "nodeId": node_id,
        "value": serialize_value(data_value.Value.Value if data_value.Value is not None else None),
        "status": status.name if status is not None else "Good",
        "isGood": status.is_good() if status is not None else True,
        "sourceTimestamp": data_value.SourceTimestamp.isoformat() if data_value.SourceTimestamp else None,
        "serverTimestamp": data_value.ServerTimestamp.isoformat() if data_value.ServerTimestamp else None,
    }
