import pytest

from app.live_frame import (
    collector_data_url,
    flatten_frame,
    frame_from_collector,
    frame_from_mapping,
    series_key,
    validate_collector_url,
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
    row = flatten_frame(frame)
    assert row[power_key] == 3.0
    assert row["AasId"] == "urn:example:aas:hyundai:hh7:pickplace:01"


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


def test_flatten_and_collector_url():
    frame = frame_from_collector(
        {
            "Meta": {"Timestamp": "t", "Seq": 1, "Model": "M", "Online": 1},
            "Status": {"Playback": 1},
            "Joint": {"J1": {"Position": 1, "Velocity": 2}},
            "Cartesian": {},
            "Errors": {"LastError": ""},
        }
    )
    row = flatten_frame(frame)
    assert row["Timestamp"] == "t"
    assert row["Joint/J1.Position"] == 1
    assert validate_collector_url("http://127.0.0.1:4100") == "http://127.0.0.1:4100"
    assert collector_data_url("http://127.0.0.1:4100") == "http://127.0.0.1:4100/api/data"
    assert collector_data_url("http://127.0.0.1:4100/api/data") == "http://127.0.0.1:4100/api/data"
    with pytest.raises(ValueError):
        validate_collector_url("file:///tmp/x")
