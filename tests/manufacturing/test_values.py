import pytest

from app.manufacturing.values import ValueRejected, normalize, quality_class


@pytest.mark.parametrize(
    ("data_type", "value", "expected"),
    [
        ("Double", 1.25, (1.25, None, None)),
        ("Double", 3, (3.0, None, None)),
        ("Float", float("nan"), None),  # NaN 도 실제 값이면 저장 (아래에서 따로 확인)
        ("Int32", 7, (7.0, None, None)),
        ("Int32", 7.0, (7.0, None, None)),
        ("Boolean", True, (None, True, None)),
        ("String", "AUTO", (None, None, "AUTO")),
        ("DateTime", "2026-11-02T00:00:00Z", (None, None, "2026-11-02T00:00:00Z")),
        ("Double", None, (None, None, None)),
        ("Int64", 2**60, (None, None, str(2**60))),  # double 로 정확히 못 담는 정수는 문자열로 무손실 저장
    ],
)
def test_normalize_accepts(data_type, value, expected):
    result = normalize(data_type, value)
    if expected is None:
        assert result[0] != result[0] and result[1] is None and result[2] is None  # NaN
    else:
        assert result == expected


@pytest.mark.parametrize(
    ("data_type", "value", "reason"),
    [
        ("Boolean", 1, "type_mismatch"),
        ("Double", True, "type_mismatch"),
        ("Double", "1.5", "type_mismatch"),
        ("String", 5, "type_mismatch"),
        ("Int32", 1.5, "type_mismatch"),
        ("Int16", 40000, "out_of_range"),
        ("UInt32", -1, "out_of_range"),
        ("Variant", 1, "unsupported_type"),
    ],
)
def test_normalize_rejects(data_type, value, reason):
    with pytest.raises(ValueRejected) as exc:
        normalize(data_type, value)
    assert str(exc.value) == reason


def test_quality_class():
    assert quality_class(0) == "GOOD"
    assert quality_class(0x40000000) == "UNCERTAIN"
    assert quality_class(0x80310000) == "BAD"
    assert quality_class(0xC0000000) == "BAD"


