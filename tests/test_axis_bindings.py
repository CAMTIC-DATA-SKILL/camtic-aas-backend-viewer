from pathlib import Path

from app.repository import load_package
from app.simulation import axis_bindings_from_package

HH7 = Path(__file__).resolve().parents[1] / "data/aasx/HH7_AAS_Template_v0.2.aasx"
DOOSAN = Path(__file__).resolve().parents[1] / "data/aasx/Doosan_M1509_AAS_Template_v0.2.aasx"


def test_hh7_bindings_use_kinematic_submodel_properties():
    if not HH7.is_file():
        return
    package = load_package(HH7, "HH7_AAS_Template_v0.2")
    axes = axis_bindings_from_package(package)
    assert axes["submodelIdShort"] == "RobotKinematicState"
    bindings = axes["bindings"]
    assert bindings["J1"]["path"] == "JointPosition.J1"
    assert bindings["J6"]["path"] == "JointPosition.J6"
    assert bindings["X"]["path"] == "TCPPosition.X"
    assert bindings["Z"]["path"] == "TCPPosition.Z"
    assert bindings["Rx"]["path"] == "TCPOrientation.Component1"
    assert bindings["Rz"]["path"] == "TCPOrientation.Component3"
    assert all(item["submodelIdShort"] == "RobotKinematicState" for item in bindings.values())
    assert all("JointSpeed" not in item["path"] for item in bindings.values())
    keys = {item["key"] for item in axes["candidates"]}
    assert "RobotKinematicState/JointPosition.J1" in keys
    assert "RobotKinematicState/JointSpeed.J1" in keys


def test_doosan_bindings_follow_same_submodel_shape():
    if not DOOSAN.is_file():
        return
    package = load_package(DOOSAN, "Doosan_M1509_AAS_Template_v0.2")
    axes = axis_bindings_from_package(package)
    assert axes["submodelIdShort"] == "RobotKinematicState"
    assert axes["bindings"]["J1"]["path"] == "JointPosition.J1"
    assert axes["bindings"]["X"]["path"] == "TCPPosition.X"
