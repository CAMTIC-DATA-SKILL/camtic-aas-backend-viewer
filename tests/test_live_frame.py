from app.live_frame import (
    frame_from_mapping,
    series_key,
)


def test_frame_from_mapping_uses_aas_paths():
    values = [
        {
            "id": "power",
            "submodelIdShort": "OperationalData",
            "path": "ActualDataMonitoring.EquipmentState.PowerYesno",
            "semanticId": "Https://www.smart-factory.kr/ids/cd/PowerYesno/1/0",
            "value": True,
            "rawValue": 3,
            "transform": "as_bool",
            "usable": True,
            "isGood": True,
        },
        {
            "id": "operation-type",
            "submodelIdShort": "OperationalData",
            "path": "ActualDataMonitoring.EquipmentState.OperationType",
            "value": "run",
            "rawValue": 1,
            "transform": "robot_status",
            "usable": True,
            "isGood": True,
        },
        {
            "id": "timestamp",
            "submodelIdShort": "OperationalData",
            "path": "Timestamp",
            "value": "2026-10-26T18:13:19",
            "transform": "compose_datetime",
            "usable": True,
            "isGood": True,
        },
        {
            "id": "j1-pos",
            "submodelIdShort": "OperationalData",
            "path": "Joint.J1.Position",
            "value": 12.5,
            "usable": True,
            "isGood": True,
        },
    ]
    frame = frame_from_mapping(
        values,
        connected=True,
        package_id="demo",
        aas_id="urn:example:aas:hyundai:hh7:pickplace:01",
    )
    assert frame["aasId"] == "urn:example:aas:hyundai:hh7:pickplace:01"
    assert frame["timestamp"] == "2026-10-26T18:13:19"
    assert frame["snapshot"]["OperationalData"]["Timestamp"] == "2026-10-26T18:13:19"
    assert frame["snapshot"]["OperationalData"]["ActualDataMonitoring"]["EquipmentState"]["PowerYesno"] is True
    assert frame["joint"]["J1"]["Position"] == 12.5
    by_id = {item["id"]: item for item in frame["series"]}
    power_key = series_key("OperationalData", "ActualDataMonitoring.EquipmentState.PowerYesno")
    assert power_key in by_id
    assert by_id[power_key]["value"] == 3.0
    assert by_id[power_key]["label"] == "PowerYesno"
    assert by_id[power_key]["group"] == "OperationalData · ActualDataMonitoring · EquipmentState"
    assert "OperationalData/Timestamp" not in by_id


def test_frame_from_hh7_template_paths():
    values = [
        {
            "id": "j1-pos",
            "submodelIdShort": "RobotKinematicState",
            "path": "JointPosition.J1",
            "value": -82.4,
            "usable": True,
            "isGood": True,
        },
        {
            "id": "j1-vel",
            "submodelIdShort": "RobotKinematicState",
            "path": "JointSpeed.J1",
            "value": 30.1,
            "usable": True,
            "isGood": True,
        },
        {
            "id": "tcp-x",
            "submodelIdShort": "RobotKinematicState",
            "path": "TCPPosition.X",
            "value": 82.7,
            "usable": True,
            "isGood": True,
        },
        {
            "id": "tcp-ori-1",
            "submodelIdShort": "RobotKinematicState",
            "path": "TCPOrientation.Component1",
            "value": -179.9,
            "usable": True,
            "isGood": True,
        },
    ]
    frame = frame_from_mapping(values, connected=True, package_id="HH7_AAS_Template_v0.2")
    assert frame["joint"]["J1"] == {"Position": -82.4, "Velocity": 30.1}
    assert frame["cartesian"]["X"] == 82.7
    assert frame["cartesian"]["Rx"] == -179.9
