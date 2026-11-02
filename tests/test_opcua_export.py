from pathlib import Path

from app.opcua_export import _path_identifier, _string_node_id, build_node_records
from app.opcua_mapping import rewrite_node_id
from app.repository import load_package

REPO_ROOT = Path(__file__).resolve().parents[1]
AASX_DIR = REPO_ROOT / "data" / "aasx"


def test_path_identifier_strips_slash_and_special_chars():
    assert _path_identifier("/HH7_AAS_Template_v0.2/HH7/JointPosition/J1") == (
        "HH7_AAS_Template_v0.2/HH7/JointPosition/J1"
    )
    assert _path_identifier("/PLC_AAS_Template_v0.4 (1)/PLC") == "PLC_AAS_Template_v0.4_1_/PLC"


def test_string_node_id_is_unique_and_rewritable():
    used: set[str] = set()
    first = _string_node_id("/HH7/RobotKinematicState/JointPosition/J1", used)
    second = _string_node_id("/HH7/RobotKinematicState/JointPosition/J1", used)
    assert first == "ns=1;s=HH7/RobotKinematicState/JointPosition/J1"
    assert second == "ns=1;s=HH7/RobotKinematicState/JointPosition/J1_2"
    assert rewrite_node_id(first, 2) == "ns=2;s=HH7/RobotKinematicState/JointPosition/J1"


def test_hh7_and_doosan_joint_position_node_ids_do_not_collide():
    hh7 = load_package(AASX_DIR / "HH7_AAS_Template_v0.2.aasx", "HH7_AAS_Template_v0.2")
    doosan = load_package(
        AASX_DIR / "Doosan_M1509_AAS_Template_v0.2.aasx",
        "Doosan_M1509_AAS_Template_v0.2",
    )
    assert hh7.error is None
    assert doosan.error is None

    hh7_ids = {record.node_id for record in build_node_records(hh7)}
    doosan_ids = {record.node_id for record in build_node_records(doosan)}
    assert all(node_id.startswith("ns=1;s=") for node_id in hh7_ids | doosan_ids)
    assert hh7_ids.isdisjoint(doosan_ids)

    hh7_j1 = next(
        record
        for record in build_node_records(hh7)
        if record.aas_path.endswith("/RobotKinematicState/JointPosition/J1")
    )
    doosan_j1 = next(
        record
        for record in build_node_records(doosan)
        if record.aas_path.endswith("/RobotKinematicState/JointPosition/J1")
    )
    assert hh7_j1.node_id == (
        "ns=1;s=HH7_AAS_Template_v0.2/HH7/RobotKinematicState/JointPosition/J1"
    )
    assert doosan_j1.node_id == (
        "ns=1;s=Doosan_M1509_AAS_Template_v0.2/Doosan_M1509_T1Z5B7/"
        "RobotKinematicState/JointPosition/J1"
    )
    assert rewrite_node_id(hh7_j1.node_id, 2) != rewrite_node_id(doosan_j1.node_id, 2)
