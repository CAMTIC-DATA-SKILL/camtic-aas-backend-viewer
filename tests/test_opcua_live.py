import asyncio
from types import SimpleNamespace

from app.opc_ua.client.client import OpcUaClient
from app.opc_ua.session import OpcUaSession
from app.opcua_api import _tag_live_nodes, engineering_live_response, owns_live_subscription, reconnect_catalog


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
    assert result["subscribePackageIds"] == [HH7]
    assert HH7 in result["message"]
    assert result["latestCount"] == 1


def test_two_packages_read_one_cache_by_tag():
    session = _session_with_hh7_cache()
    session._subscribe_package_ids = [HH7, "PLC_AAS_Template_v0.4 (1)"]
    assert owns_live_subscription(session, HH7) is True
    assert owns_live_subscription(session, "PLC_AAS_Template_v0.4 (1)") is True
    assert owns_live_subscription(session, DOOSAN) is False

    tag = "ns=2;s=D2010"
    session._latest[tag] = {"nodeId": tag, "value": True, "status": "Good", "isGood": True}
    package_id = "PLC_AAS_Template_v0.4 (1)"
    nodes = _tag_live_nodes(
        [
            {
                "aasPath": f"/{package_id}/PLC_AAS/IOInterfaceMapping/Map_D2010/PLCAddress",
                "templateValue": "D2010",
            },
            {
                "aasPath": f"/{package_id}/PLC_AAS/IOInterfaceMapping/Map_D2010/AASPath",
                "templateValue": "Robot02_Delta/Handshake/GripWaitRequest",
            },
        ],
        package_id,
    )
    result = engineering_live_response(nodes, session, package_id, field_namespace=2)
    assert result["subscribeMatch"] is True
    assert result["values"][0]["path"] == "Handshake.GripWaitRequest"
    assert result["values"][0]["submodelIdShort"] == "Robot02_Delta"
    assert result["values"][0]["value"] is True
    assert result["values"][0]["nodeId"] == tag
    assert len(session.latest_values()) == 2


def _state(value: str):
    return SimpleNamespace(value=value)


def test_transport_connected_follows_socket_state():
    client = OpcUaClient.__new__(OpcUaClient)
    client._client = None
    assert client.is_transport_connected is False
    client._client = SimpleNamespace(uaclient=SimpleNamespace(state=_state("reconnecting")))
    assert client.is_transport_connected is False
    client._client.uaclient.state = _state("connected")
    assert client.is_transport_connected is True


def test_reconnect_catalog_keeps_a_live_subscription(monkeypatch):
    async def fail_connect(*_args, **_kwargs):
        raise AssertionError("live session should not reconnect")

    monkeypatch.setattr("app.opcua_api.connect_from_catalog", fail_connect)
    session = OpcUaSession()
    session._client = SimpleNamespace(
        is_connected=True,
        is_transport_connected=True,
        config=session._default_cfg,
    )
    session._subscribed = ["ns=2;s=J1"]

    status = asyncio.run(reconnect_catalog(session, None))

    assert status["connected"] is True
    assert status["subscribedNodeIds"] == ["ns=2;s=J1"]


def test_reconnect_catalog_drops_a_dead_session_and_uses_the_catalog(monkeypatch):
    events = []

    class Dead:
        is_connected = True
        is_transport_connected = False

        async def disconnect(self):
            events.append("disconnect")

    async def fake_connect(_session, _repository, **kwargs):
        events.append(kwargs["use_default_server"])
        return {"connected": True}

    monkeypatch.setattr("app.opcua_api.connect_from_catalog", fake_connect)
    session = OpcUaSession()
    session._client = Dead()

    status = asyncio.run(reconnect_catalog(session, None))

    assert status["connected"] is True
    assert events == ["disconnect", True]
    assert session._client is None
