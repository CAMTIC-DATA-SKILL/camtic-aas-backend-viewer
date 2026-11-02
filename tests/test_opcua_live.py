from app.opc_ua.session import OpcUaSession
from app.opcua_api import engineering_live_response, owns_live_subscription


HH7 = "HH7_AAS_Template_v0.2"
DOOSAN = "Doosan_M1509_AAS_Template_v0.2"
HH7_J1 = f"ns=2;s={HH7}/HH7/RobotKinematicState/JointPosition/J1"


def _session_with_hh7_cache() -> OpcUaSession:
    session = OpcUaSession()
    session._subscribe_package_id = HH7
    session._subscribed = [HH7_J1]
    session._latest = {
        HH7_J1: {
            "nodeId": HH7_J1,
            "value": -82.4,
            "status": "Good",
            "isGood": True,
        }
    }
    return session


def test_owns_live_subscription_requires_exact_package():
    session = OpcUaSession()
    assert owns_live_subscription(session, HH7) is False
    session._subscribe_package_id = HH7
    assert owns_live_subscription(session, HH7) is True
    assert owns_live_subscription(session, DOOSAN) is False
    assert owns_live_subscription(session, "Hyundai_HH7_AAS_Template_v0.3") is False


def test_engineering_live_keeps_subscribed_package_values():
    session = _session_with_hh7_cache()
    nodes = [
        {
            "nodeId": f"ns=1;s={HH7}/HH7/RobotKinematicState/JointPosition/J1",
            "browseName": "J1",
            "aasPath": f"/{HH7}/HH7/RobotKinematicState/JointPosition/J1",
        }
    ]
    result = engineering_live_response(nodes, session, HH7, field_namespace=2)
    assert result["subscribeMatch"] is True
    assert result["values"][0]["value"] == -82.4
    assert result["values"][0]["path"] == "JointPosition.J1"


def test_engineering_live_hides_cache_from_other_package():
    session = _session_with_hh7_cache()
    nodes = [
        {
            "nodeId": f"ns=1;s={DOOSAN}/Doosan_M1509_T1Z5B7/RobotKinematicState/JointPosition/J1",
            "browseName": "J1",
            "aasPath": f"/{DOOSAN}/Doosan_M1509_T1Z5B7/RobotKinematicState/JointPosition/J1",
        }
    ]
    result = engineering_live_response(nodes, session, DOOSAN, field_namespace=2)
    assert result["subscribeMatch"] is False
    assert result["values"] == []
    assert result["frame"]["online"] is False
    assert result["subscribePackageId"] == HH7
    assert HH7 in result["message"]
    assert result["latestCount"] == 1
