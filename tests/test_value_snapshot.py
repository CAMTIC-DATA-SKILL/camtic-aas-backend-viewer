import io
from pathlib import Path

from basyx.aas import model
from basyx.aas.adapter import aasx
from basyx.aas.adapter.json import read_aas_json_file

from app.api import _serialize_package
from app.opc_ua.session import OpcUaSession
from app.opcua_api import captured_property_values
from app.opcua_export import generate_opcua_artifacts, load_engineering_nodes
from app.repository import load_package
from app.value_snapshot import apply_captured_values, capture_object_store

ROOT = Path(__file__).resolve().parents[1]
HH7_PATH = ROOT / "data" / "packages" / "HH7_AAS_Template_v0.2" / "package.aasx"
HH7_ID = "HH7_AAS_Template_v0.2"


def _property(store, submodel_name: str, path: str) -> model.Property:
    submodel = next(
        obj for obj in store if isinstance(obj, model.Submodel) and obj.id_short == submodel_name
    )
    node = submodel
    for part in path.split("."):
        children = list(node.submodel_element if isinstance(node, model.Submodel) else node.value)
        node = next(child for child in children if child.id_short == part)
    assert isinstance(node, model.Property)
    return node


def _kinematic_store() -> tuple[model.DictIdentifiableStore, model.Property]:
    joint = model.Property("J1", model.datatypes.Double)
    speed = model.Property("TCPSpeed", model.datatypes.Double)
    enabled = model.Property("Enabled", model.datatypes.Boolean)
    submodel = model.Submodel(
        "urn:test:kinematic",
        id_short="RobotKinematicState",
        submodel_element=[
            model.SubmodelElementCollection("JointPosition", [joint]),
            speed,
            enabled,
        ],
    )
    return model.DictIdentifiableStore([submodel]), joint


def test_apply_casts_numbers_and_keeps_zero():
    store, joint = _kinematic_store()
    applied = apply_captured_values(
        store,
        [
            {"submodelIdShort": "RobotKinematicState", "path": "JointPosition.J1", "value": -82},
            {"submodelIdShort": "RobotKinematicState", "path": "TCPSpeed", "value": 0},
            {"submodelIdShort": "RobotKinematicState", "path": "Enabled", "value": False},
            {"submodelIdShort": "RobotKinematicState", "path": "Missing.J9", "value": 1},
        ],
    )
    assert applied == 3
    assert joint.value == -82.0
    assert _property(store, "RobotKinematicState", "TCPSpeed").value == 0.0
    assert _property(store, "RobotKinematicState", "Enabled").value is False


def test_capture_clones_before_writing():
    store, joint = _kinematic_store()
    cloned, applied = capture_object_store(
        store,
        [{"submodelIdShort": "RobotKinematicState", "path": "JointPosition.J1", "value": 1.25}],
    )
    assert applied == 1
    assert joint.value is None
    assert cloned is not None
    assert _property(cloned, "RobotKinematicState", "JointPosition.J1").value == 1.25


class _Artifacts:
    def __init__(self, path: Path):
        self.path = path

    def artifact_dir(self, package_id: str) -> Path:
        return self.path


def _hh7_session(node_id: str, value, *, good: bool = True) -> OpcUaSession:
    session = OpcUaSession()
    session._subscribe_package_id = HH7_ID
    session._subscribed = [node_id]
    session._latest = {
        node_id: {
            "nodeId": node_id,
            "value": value,
            "status": "Good" if good else "Bad",
            "isGood": good,
        }
    }
    return session


def test_download_writes_subscribed_values_without_touching_the_package(tmp_path: Path):
    package = load_package(HH7_PATH, HH7_ID)
    assert package.error is None
    original_bytes = HH7_PATH.read_bytes()
    assert _property(package.objects, "RobotKinematicState", "JointPosition.J1").value is None
    assert _property(package.objects, "RobotOperationalState", "ProgramTaskState").value is None

    generate_opcua_artifacts(package, tmp_path)
    nodes = load_engineering_nodes(tmp_path, variables_only=True)
    by_path = {node["aasPath"]: node["nodeId"] for node in nodes}
    j1_id = by_path[f"/{HH7_ID}/HH7/RobotKinematicState/JointPosition/J1"]
    task_id = by_path[f"/{HH7_ID}/HH7/RobotOperationalState/ProgramTaskState"]
    bad_id = by_path[f"/{HH7_ID}/HH7/RobotKinematicState/JointPosition/J2"]

    session = _hh7_session(j1_id, -82.4)
    session._subscribed = [j1_id, task_id, bad_id]
    session._latest[task_id] = {
        "nodeId": task_id,
        "value": "AUTO",
        "status": "Good",
        "isGood": True,
    }
    session._latest[bad_id] = {
        "nodeId": bad_id,
        "value": 99.0,
        "status": "Bad",
        "isGood": False,
    }
    repository = _Artifacts(tmp_path)

    captured = captured_property_values(package, repository, session)
    assert {item["path"]: item["value"] for item in captured}["JointPosition.J1"] == -82.4
    assert {item["path"]: item["value"] for item in captured}["ProgramTaskState"] == "AUTO"
    assert all(item["path"] != "JointPosition.J2" for item in captured)

    response = _serialize_package(package, "json", repository, session)
    assert response.headers["X-Captured-Values"] == str(len(captured))
    assert "HH7_AAS_Template_v0.2_snapshot.json" in response.headers["content-disposition"]
    restored = read_aas_json_file(io.StringIO(response.body.decode("utf-8")), failsafe=False)
    assert _property(restored, "RobotKinematicState", "JointPosition.J1").value == -82.4
    assert _property(restored, "RobotOperationalState", "ProgramTaskState").value == "AUTO"
    assert _property(restored, "RobotKinematicState", "JointPosition.J2").value is None

    xml = _serialize_package(package, "xml", repository, session)
    assert b"-82.4" in xml.body

    packaged = _serialize_package(package, "aasx", repository, session)
    reloaded = model.DictIdentifiableStore()
    with aasx.AASXReader(io.BytesIO(packaged.body)) as reader:
        reader.read_into(reloaded, aasx.DictSupplementaryFileContainer())
    assert _property(reloaded, "RobotKinematicState", "JointPosition.J1").value == -82.4

    assert _property(package.objects, "RobotKinematicState", "JointPosition.J1").value is None
    assert HH7_PATH.read_bytes() == original_bytes


def test_download_without_subscription_returns_the_original_file():
    package = load_package(HH7_PATH, HH7_ID)
    response = _serialize_package(package, "aasx", _Artifacts(HH7_PATH.parent), OpcUaSession())
    assert response.headers["X-Captured-Values"] == "0"
    assert Path(response.path) == HH7_PATH
