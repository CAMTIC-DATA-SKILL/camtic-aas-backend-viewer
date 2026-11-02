"""수신값 검증·정규화. 값은 변환·반올림하지 않고 타입에 맞는 컬럼에 그대로 넣는다."""

from __future__ import annotations

from typing import Any, Optional

NUMERIC_TYPES = frozenset(
    {"SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64", "Float", "Double"}
)
INTEGER_TYPES = frozenset({"SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"})
TEXT_TYPES = frozenset({"String", "DateTime"})
BOOL_TYPE = "Boolean"

_INT_RANGES = {
    "SByte": (-(2**7), 2**7 - 1),
    "Byte": (0, 2**8 - 1),
    "Int16": (-(2**15), 2**15 - 1),
    "UInt16": (0, 2**16 - 1),
    "Int32": (-(2**31), 2**31 - 1),
    "UInt32": (0, 2**32 - 1),
    "Int64": (-(2**63), 2**63 - 1),
    "UInt64": (0, 2**64 - 1),
}
# double 로 정확히 표현되는 정수 범위. 이보다 큰 Int64/UInt64 는 손실 없이 value_text 에 넣는다.
_EXACT_INT = 2**53

QUALITY_MAX = 2**32 - 1

Normalized = tuple[Optional[float], Optional[bool], Optional[str]]


class ValueRejected(ValueError):
    """이 샘플은 저장하지 않는다. str(exc) 가 거부 사유 코드다."""


def normalize(data_type: str, value: Any) -> Normalized:
    """(value_num, value_bool, value_text). value 가 None 이면 모두 None (Bad 품질 등)."""
    if value is None:
        return None, None, None
    if data_type == BOOL_TYPE:
        if not isinstance(value, bool):
            raise ValueRejected("type_mismatch")
        return None, value, None
    if data_type in TEXT_TYPES:
        if not isinstance(value, str):
            raise ValueRejected("type_mismatch")
        return None, None, value
    if data_type in NUMERIC_TYPES:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueRejected("type_mismatch")
        bounds = _INT_RANGES.get(data_type)
        if bounds is not None:
            if isinstance(value, float):
                if not value.is_integer():
                    raise ValueRejected("type_mismatch")
                value = int(value)
            if not bounds[0] <= value <= bounds[1]:
                raise ValueRejected("out_of_range")
            if abs(value) > _EXACT_INT:
                return None, None, str(value)
            return float(value), None, None
        # Float/Double: NaN·Infinity 도 실제 값이면 그대로 저장한다 (PostgreSQL double 이 표현함).
        return float(value), None, None
    raise ValueRejected("unsupported_type")


def quality_class(code: int) -> str:
    """OPC-UA StatusCode 상위 2비트: 00 Good, 01 Uncertain, 10/11 Bad."""
    severity = (code >> 30) & 0b11
    if severity == 0:
        return "GOOD"
    if severity == 1:
        return "UNCERTAIN"
    return "BAD"
