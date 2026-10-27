import json
from pathlib import Path

from app.opcua_mapping import (
    apply_engineering,
    apply_mapping,
    as_bool,
    compose_datetime,
    load_mapping,
    rewrite_node_id,
    robot_status,
    split_engineering_path,
)


def test_compose_datetime():
    assert compose_datetime(
        {"year": 2026, "month": 10, "day": 26, "hour": 18, "minute": 13, "second": 19}
    ) == "2026-10-26T18:13:19"
    assert compose_datetime({"year": 2026, "month": 13, "day": 1}) is None


def test_as_bool_and_robot_status():
    assert as_bool(3) is True
    assert as_bool(0) is False
    assert robot_status(0) == "stop"
    assert robot_status(1) == "run"
    assert robot_status(2) == "wait"


def test_load_and_apply_hyundai_mapping(tmp_path: Path):
    mapping = load_mapping(
        "Hyundai_HH7_PickPlace_AdsorptionGripper_InstanceAAS_v0.2_fixed (1)",
        tmp_path,
    )
    assert mapping is not None
    assert len(mapping.bindings) == 3
    assert mapping.required_node_ids() == [
        "ns=2;s=robot.robot-1.S0004",
        "ns=2;s=robot.robot-1.S0041",
        "ns=2;s=robot.robot-1.S0006",
        "ns=2;s=robot.robot-1.S0008",
        "ns=2;s=robot.robot-1.S0009",
        "ns=2;s=robot.robot-1.S0010",
        "ns=2;s=robot.robot-1.S0011",
        "ns=2;s=robot.robot-1.S0012",
    ]

    live = [
        {"nodeId": "ns=2;s=robot.robot-1.S0004", "value": 3, "status": "Good", "isGood": True},
        {"nodeId": "ns=2;s=robot.robot-1.S0041", "value": 0, "status": "Good", "isGood": True},
        {"nodeId": "ns=2;s=robot.robot-1.S0006", "value": 2026, "status": "Good", "isGood": True},
        {"nodeId": "ns=2;s=robot.robot-1.S0008", "value": 10, "status": "Good", "isGood": True},
        {"nodeId": "ns=2;s=robot.robot-1.S0009", "value": 26, "status": "Good", "isGood": True},
        {"nodeId": "ns=2;s=robot.robot-1.S0010", "value": 18, "status": "Good", "isGood": True},
        {"nodeId": "ns=2;s=robot.robot-1.S0011", "value": 13, "status": "Good", "isGood": True},
        {"nodeId": "ns=2;s=robot.robot-1.S0012", "value": 19, "status": "Good", "isGood": True},
    ]
    values = {item["path"]: item for item in apply_mapping(mapping, live)}
    assert values["ActualDataMonitoring.EquipmentState.PowerYesno"]["value"] is True
    assert values["ActualDataMonitoring.EquipmentState.OperationType"]["value"] == "stop"
    assert values["Timestamp"]["value"] == "2026-10-26T18:13:19"
    assert values["Timestamp"]["isGood"] is True
    assert values["Timestamp"]["usable"] is True

    stale = [{**item, "isGood": False, "status": "UncertainLastUsableValue"} for item in live]
    stale_values = {item["path"]: item for item in apply_mapping(mapping, stale)}
    assert stale_values["Timestamp"]["value"] == "2026-10-26T18:13:19"
    assert stale_values["Timestamp"]["usable"] is True
    assert stale_values["Timestamp"]["isGood"] is False


def test_artifact_override_wins(tmp_path: Path):
    override = {
        "packageId": "demo",
        "bindings": [
            {
                "id": "only",
                "submodelIdShort": "OperationalData",
                "path": "Timestamp",
                "nodeId": "ns=2;s=demo",
            }
        ],
    }
    (tmp_path / "mapping.json").write_text(json.dumps(override), encoding="utf-8")
    mapping = load_mapping(
        "Hyundai_HH7_PickPlace_AdsorptionGripper_InstanceAAS_v0.2_fixed (1)",
        tmp_path,
    )
    assert mapping is not None
    assert mapping.bindings[0].node_id == "ns=2;s=demo"


def test_split_engineering_path():
    package_id = "Hyundai_HH7_PickPlace_AdsorptionGripper_InstanceAAS_v0.2_fixed (1)"
    submodel, path = split_engineering_path(
        f"/{package_id}/HyundaiHH7PickPlaceRobot01/OperationalData/"
        "ActualDataMonitoring/EquipmentState/PowerYesno",
        package_id,
    )
    assert submodel == "OperationalData"
    assert path == "ActualDataMonitoring.EquipmentState.PowerYesno"


def test_rewrite_node_id():
    assert rewrite_node_id("ns=1;i=1145", 2) == "ns=2;i=1145"
    assert rewrite_node_id("ns=1;s=HH7/J1", 2) == "ns=2;s=HH7/J1"
    assert rewrite_node_id("ns=2;i=1145", 2) == "ns=2;i=1145"
    assert rewrite_node_id("ns=1;i=1145", None) == "ns=1;i=1145"


def test_apply_engineering_joins_live_values():
    package_id = "pkg"
    nodes = [
        {
            "nodeId": "ns=1;i=1008",
            "browseName": "PowerYesno",
            "displayName": "PowerYesno",
            "aasPath": f"/{package_id}/Robot01/OperationalData/ActualDataMonitoring/PowerYesno",
            "semanticId": "https://example/PowerYesno",
        }
    ]
    mapped = apply_engineering(
        nodes,
        [{"nodeId": "ns=1;i=1008", "value": 1, "status": "Good", "isGood": True}],
        package_id=package_id,
    )
    assert mapped[0]["submodelIdShort"] == "OperationalData"
    assert mapped[0]["path"] == "ActualDataMonitoring.PowerYesno"
    assert mapped[0]["value"] == 1
    assert mapped[0]["usable"] is True

    remapped = apply_engineering(
        nodes,
        [{"nodeId": "ns=2;i=1008", "value": 7, "status": "Good", "isGood": True}],
        package_id=package_id,
        field_namespace=2,
    )
    assert remapped[0]["nodeId"] == "ns=2;i=1008"
    assert remapped[0]["value"] == 7
