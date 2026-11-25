import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from config import BASE_DIR, MqttConfig
from app.main import persistent_opcua_subscription
from app.mqtt_api import subscriber_view
from app.mqtt_feed import MqttFeed
from app.mqtt_messages import (
    decode_payload,
    encode_payload,
    load_publish_plan,
    resolve_wildcard,
    branch_choices,
    suggested_wildcards,
    topic_from_aas_path,
    topic_matches,
)
from app.opc_ua.client import DataChange
from app.opc_ua.session import OpcUaSession
from app.opcua_api import engineering_live_response, utility_connect_subscribes
from publisher.outbound import TopicOutbound
from publisher.service import queue_change


HH7 = "Hyundai_HH7_AAS_Template_v0.3"
J1_PATH = f"/{HH7}/HH7/RobotKinematicState/JointPosition/J1"
J1_TOPIC = f"aas/{HH7}/HH7/RobotKinematicState/JointPosition/J1"
J1_NODE = f"ns=1;s={HH7}/HH7/RobotKinematicState/JointPosition/J1"
WHEN = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)


def _csv(node_id: str, aas_path: str, aas_id: str, node_class: str = "Variable") -> str:
    return (
        "NodeId,BrowseName,DisplayName,NodeClass,DataType,ParentNodeId,AasPath,AasId,ModelType,SemanticId,ValueType,Value,AccessLevel\n"
        f"{node_id},J1,J1,{node_class},Double,,{aas_path},{aas_id},Property,,,\n"
    )


def test_topic_and_payload_follow_the_aas_path():
    assert topic_from_aas_path(J1_PATH, "aas") == J1_TOPIC
    raw = encode_payload(
        package_id=HH7,
        path="RobotKinematicState/JointPosition/J1",
        node_id=J1_NODE,
        value=-82.4,
        status="Good",
        source_timestamp=WHEN,
        status_code=0,
    )
    body = json.loads(raw)
    assert list(body) == [
        "packageId",
        "path",
        "nodeId",
        "value",
        "status",
        "sourceTimestamp",
        "statusCode",
    ]
    assert body["value"] == -82.4
    assert body["sourceTimestamp"] == "2026-11-10T04:00:00Z"
    update = decode_payload(raw)
    assert update is not None
    assert update.package_id == HH7
    assert update.source_timestamp == WHEN
    assert update.status_code == 0


def test_decode_fills_quality_when_status_code_is_omitted():
    good = decode_payload(
        json.dumps(
            {
                "packageId": HH7,
                "path": "RobotKinematicState/JointPosition/J1",
                "nodeId": J1_NODE,
                "value": 1,
                "status": "Good",
                "sourceTimestamp": "2026-11-10T04:00:00Z",
            }
        )
    )
    bad = decode_payload(
        json.dumps(
            {
                "packageId": HH7,
                "path": "RobotKinematicState/JointPosition/J1",
                "nodeId": J1_NODE,
                "value": 1,
                "status": "Bad",
            }
        )
    )
    assert good is not None and good.status_code == 0
    assert bad is not None and bad.status_code == 0x80000000
    assert decode_payload(b"not-json") is None
    assert decode_payload(b"{}") is None


def test_plan_rewrites_namespace_and_skips_other_aas(tmp_path: Path):
    opcua = tmp_path / "Pkg" / "opcua"
    opcua.mkdir(parents=True)
    (opcua / "engineering.csv").write_text(
        _csv(
            "ns=1;s=Pkg/Shell/RobotKinematicState/JointPosition/J1",
            "/Pkg/Shell/RobotKinematicState/JointPosition/J1",
            "urn:example",
        )
        + "ns=1;s=Pkg,Pkg,Pkg,Object,,,/Pkg,urn:example,AasxPackage,,,\n",
        encoding="utf-8",
    )
    other = tmp_path / "Other" / "opcua"
    other.mkdir(parents=True)
    (other / "engineering.csv").write_text(
        _csv("ns=1;s=Other/Shell/RobotKinematicState/JointPosition/J1", "/Other/Shell/RobotKinematicState/JointPosition/J1", "urn:other"),
        encoding="utf-8",
    )
    plan = load_publish_plan(tmp_path, "opcua", 2, "aas", aas_ids=("urn:example",))
    assert len(plan) == 1
    assert plan[0].package_id == "Pkg"
    assert plan[0].node_id == "ns=2;s=Pkg/Shell/RobotKinematicState/JointPosition/J1"
    assert plan[0].topic == "aas/Pkg/Shell/RobotKinematicState/JointPosition/J1"
    assert plan[0].path == "RobotKinematicState/JointPosition/J1"
    assert load_publish_plan(tmp_path, "opcua", 2, "aas", aas_ids=("urn:missing",)) == []


def test_workspace_hh7_joint_is_in_the_publish_plan():
    packages = BASE_DIR / "data" / "packages"
    plan = load_publish_plan(
        packages,
        "opcua",
        1,
        "aas",
        aas_ids=("urn:physical-ai:aas:hyundai:hh7:template:v0.3",),
    )
    found = [point for point in plan if point.topic == J1_TOPIC]
    assert len(found) == 1
    assert found[0].package_id == HH7
    assert found[0].node_id == J1_NODE
    assert found[0].path == "RobotKinematicState/JointPosition/J1"
    assert all(point.package_id == HH7 for point in plan)


def test_outbound_publishes_a_changed_value_once_until_the_session_restarts():
    outbound = TopicOutbound()
    topic = J1_TOPIC
    outbound.offer(topic, "{}", "same")
    outbound.offer(topic, "{}", "same")
    assert list(outbound._pending) == [topic]
    outbound.acknowledge(topic, "same")
    assert outbound.needs(topic, "same") is False
    outbound.offer(topic, '{"value":1}', "next")
    assert outbound._pending[topic][0] == "next"
    outbound.reset_sent()
    assert outbound.needs(topic, "next") is False
    outbound.acknowledge(topic, "old")
    assert topic in outbound._pending
    outbound.acknowledge(topic, "next")
    outbound.reset_sent()
    assert outbound.needs(topic, "next") is True


def test_queue_change_skips_an_unchanged_value():
    from app.mqtt_messages import PublishPoint

    point = PublishPoint(HH7, J1_NODE, J1_TOPIC, "RobotKinematicState/JointPosition/J1")
    outbound = TopicOutbound()
    index = {J1_NODE: [point]}
    change = DataChange(J1_NODE, -82.4, "Good", True, WHEN, None, 0)
    queue_change(outbound, index, change)
    queue_change(outbound, index, change)
    assert len(outbound._pending) == 1
    payload = json.loads(outbound._pending[J1_TOPIC][1])
    assert payload["value"] == -82.4
    assert payload["nodeId"] == J1_NODE
    queued = DataChange(J1_NODE, -82.5, "Good", True, WHEN, None, 0)
    queue_change(outbound, index, queued)
    assert json.loads(outbound._pending[J1_TOPIC][1])["value"] == -82.5


def test_mqtt_message_fills_live_cache_without_an_opcua_session():
    async def run() -> None:
        session = OpcUaSession()
        session.bind_mqtt_catalog("default", [])
        seen: list[object] = []
        session.add_listener(lambda change, _received: seen.append(change.value))
        feed = MqttFeed(
            MqttConfig(enabled=True, host="127.0.0.1", port=1883, topic_prefix="aas"),
            session,
        )
        await feed.handle_message(
            encode_payload(
                package_id=HH7,
                path="RobotKinematicState/JointPosition/J1",
                node_id=J1_NODE,
                value=-82.4,
                status="Good",
                source_timestamp=WHEN,
                status_code=0,
            ).encode(),
            J1_TOPIC,
        )
        await feed.handle_message(b"not-json")
        nodes = [
            {
                "nodeId": J1_NODE,
                "browseName": "J1",
                "aasPath": J1_PATH,
            }
        ]
        session.set_mqtt_online(True)
        result = engineering_live_response(nodes, session, HH7, field_namespace=1)
        assert result["subscribeMatch"] is True
        assert result["values"][0]["value"] == -82.4
        assert result["values"][0]["path"] == "JointPosition.J1"
        assert result["frame"]["online"] is True
        assert session.is_connected is False
        assert seen == [-82.4]
        assert feed.received == 1
        assert feed.rejected == 1
        logs, matched = feed.log_entries(10)
        assert matched == 1
        assert logs[0]["accepted"] is True
        assert logs[0]["topic"] == J1_TOPIC
        assert logs[0]["value"] == -82.4
        assert logs[0]["path"] == "RobotKinematicState/JointPosition/J1"

    asyncio.run(run())


def test_env_flag_rewrite_keeps_other_lines(tmp_path):
    from config import write_env_value

    env = tmp_path / ".env"
    env.write_text("# keep\nMQTT_HOST=127.0.0.1\nMQTT_ENABLED=false\n", encoding="utf-8")
    write_env_value(env, "MQTT_ENABLED", "true")
    text = env.read_text(encoding="utf-8")
    assert "MQTT_ENABLED=true" in text
    assert "MQTT_ENABLED=false" not in text
    assert "MQTT_HOST=127.0.0.1" in text
    assert "# keep" in text


def test_handoff_to_mqtt_drops_opcua_subscription_and_keeps_cache():
    async def run() -> None:
        session = OpcUaSession()
        session._latest = {J1_NODE: {"nodeId": J1_NODE, "value": -82.4}}
        session._desired_node_ids = [J1_NODE]
        session._subscribed = [J1_NODE]
        session._subscribe_package_ids = [HH7]
        await session.handoff_to_mqtt()
        assert session.live_source == "mqtt"
        assert session.subscribed_node_ids == []
        assert session.latest_values()[0]["value"] == -82.4
        session.use_opcua_live()
        assert session.live_source == "opcua"
        assert session.mqtt_connected is False

    asyncio.run(run())


def test_subscriber_view_says_disabled_when_mqtt_is_off(monkeypatch):
    from app import mqtt_api

    cfg = MqttConfig(enabled=False, host="127.0.0.1", port=1883, topic_prefix="aas")
    monkeypatch.setattr(
        mqtt_api,
        "route_fields",
        lambda: {"liveSource": "opcua", "autoConnectOpcua": False},
    )
    body = subscriber_view(cfg, None)
    assert body["enabled"] is False
    assert body["subscriber"] == "disabled"
    assert body["connected"] is False
    assert "OPC-UA" in body["message"]
    assert body["liveSource"] == "opcua"
    assert body["view"] == "latest"
    assert body["wildcard"] == ""
    assert body["branches"] == []
    assert body["entries"] == []
    assert body["topic"] == "aas/#"


def test_utility_disconnect_keeps_mqtt_values():
    async def run() -> None:
        session = OpcUaSession()
        session.bind_mqtt_catalog("default", [HH7])
        await session.apply_field_change(DataChange(J1_NODE, -82.4, "Good", True, WHEN, None, 0))
        session._subscribed = [J1_NODE]
        session._desired_node_ids = [J1_NODE]
        await session.unsubscribe()
        await session.disconnect()
        assert session.latest_values()[0]["value"] == -82.4
        assert session.subscribe_package_ids == [HH7]
        assert session.catalog_server_id == "default"
        assert session.subscribed_node_ids == []
        assert session.is_connected is False

    asyncio.run(run())


def test_opcua_disconnect_still_clears_the_live_cache():
    async def run() -> None:
        session = OpcUaSession()
        session._latest = {J1_NODE: {"nodeId": J1_NODE, "value": 1}}
        session._subscribe_package_ids = [HH7]
        session._subscribe_package_id = HH7
        session._catalog_server_id = "default"
        await session.disconnect()
        assert session.latest_values() == []
        assert session.subscribe_package_ids == []
        assert session.catalog_server_id is None

    asyncio.run(run())


def test_empty_live_cache_is_filled_from_retained_broker_values():
    async def run() -> None:
        session = OpcUaSession()
        session.bind_mqtt_catalog("default", [HH7])
        feed = MqttFeed(
            MqttConfig(enabled=True, host="127.0.0.1", port=1883, topic_prefix="aas"),
            session,
        )
        feed._plan_loaded = True
        feed._published = {J1_NODE: J1_TOPIC}
        calls: list[set[str]] = []

        async def fake_fetch(missing: set[str]) -> set[str]:
            calls.append(set(missing))
            session.remember_retained(DataChange(J1_NODE, -61.5, "Good", True, WHEN, None, 0))
            return {J1_NODE}

        feed._fetch_retained = fake_fetch  # type: ignore[method-assign]
        assert await feed.fill_missing_from_broker() == 1
        assert calls == [{J1_NODE}]
        assert session.latest_values()[0]["value"] == -61.5
        assert await feed.fill_missing_from_broker() == 0
        assert calls == [{J1_NODE}]

        session._latest.clear()
        feed._published["ns=1;s=not-published"] = "aas/not-published"

        async def broker_has_only_j1(missing: set[str]) -> set[str]:
            calls.append(set(missing))
            session.remember_retained(DataChange(J1_NODE, -61.5, "Good", True, WHEN, None, 0))
            return {J1_NODE}

        feed._fetch_retained = broker_has_only_j1  # type: ignore[method-assign]
        assert await feed.fill_missing_from_broker() == 1
        assert "ns=1;s=not-published" in calls[-1]
        assert await feed.fill_missing_from_broker() == 0
        assert len(calls) == 2

    asyncio.run(run())


def test_retained_restore_does_not_call_recorders():
    session = OpcUaSession()
    seen: list[object] = []
    session.add_listener(lambda change, _received: seen.append(change.value))
    session.remember_retained(DataChange(J1_NODE, -61.5, "Good", True, WHEN, None, 0))
    assert session.latest_values()[0]["value"] == -61.5
    assert seen == []


def test_wildcard_filter_keeps_the_latest_value_of_each_matching_topic():
    async def run() -> None:
        session = OpcUaSession()
        feed = MqttFeed(
            MqttConfig(enabled=True, host="127.0.0.1", port=1883, topic_prefix="camtic"),
            session,
        )
        hh7 = "camtic/Hyundai_HH7_AAS_Template_v0.3/HH7/RobotKinematicState/JointPosition/J1"
        plc = "camtic/PLC_AAS_Template_v0.4 (1)/PLC_AAS/Robot01_Doosan/Permission/GripPermission"
        for value in (1, 2, 3):
            await feed.handle_message(_payload(HH7, "RobotKinematicState/JointPosition/J1", value), hh7)
        await feed.handle_message(
            _payload("PLC_AAS_Template_v0.4 (1)", "Robot01_Doosan/Permission/GripPermission", False),
            plc,
        )
        current, matched_all = feed.log_entries(10)
        assert matched_all == 2
        by_topic = {entry["topic"]: entry for entry in current}
        assert by_topic[hh7]["value"] == 3
        assert by_topic[plc]["value"] is False

        latest, matched = feed.log_entries(10, "camtic/PLC_AAS_Template_v0.4 (1)/#")
        assert matched == 1
        assert latest[0]["topic"] == plc
        assert latest[0]["value"] is False

        joints, joint_count = feed.log_entries(10, "camtic/+/HH7/RobotKinematicState/#")
        assert joint_count == 1
        assert joints[0]["value"] == 3

        branches = {item["package"]: item for item in feed.package_branches("camtic")}
        assert set(branches) == {"Hyundai_HH7_AAS_Template_v0.3", "PLC_AAS_Template_v0.4 (1)"}
        assert [item["name"] for item in branches["Hyundai_HH7_AAS_Template_v0.3"]["children"]] == ["HH7"]
        assert [item["name"] for item in branches["PLC_AAS_Template_v0.4 (1)"]["children"]] == ["PLC_AAS"]
        assert body_has_no_deeper_branch(feed.package_branches("camtic"))

        body = subscriber_view(feed.cfg, feed, wildcard="PLC_AAS_Template_v0.4 (1)/#")
        assert body["view"] == "latest"
        assert body["wildcard"] == "camtic/PLC_AAS_Template_v0.4 (1)/#"
        assert body["entries"][0]["topic"] == plc
        whole = subscriber_view(feed.cfg, feed, wildcard="camtic/#")
        assert whole["view"] == "latest"
        assert len(whole["entries"]) == 2
        assert subscriber_view(feed.cfg, feed, wildcard="")["wildcard"] == ""

    asyncio.run(run())


def body_has_no_deeper_branch(branches: list[dict]) -> bool:
    for branch in branches:
        for child in branch["children"]:
            if "/" in child["name"] or child["name"] in {"RobotKinematicState", "Permission", "JointPosition"}:
                return False
    return True


def test_branch_choices_keep_only_the_package_and_the_next_segment():
    topics = [f"camtic/HH7/Shell/RobotKinematicState/JointPosition/J{i}" for i in range(1, 7)]
    topics += [f"camtic/PLC/PLC_AAS/IOInterfaceMapping/Map_{i}/A" for i in range(100)]
    branches = {item["package"]: item for item in branch_choices("camtic", topics)}
    assert branches["HH7"]["topics"] == 6
    assert branches["HH7"]["children"] == [{"name": "Shell", "topics": 6}]
    assert branches["PLC"]["children"] == [{"name": "PLC_AAS", "topics": 100}]
    assert body_has_no_deeper_branch(list(branches.values()))


def test_suggested_wildcards_stop_at_a_wide_branch():
    topics = [f"camtic/HH7/Shell/RobotKinematicState/JointPosition/J{i}" for i in range(1, 7)]
    topics += [f"camtic/PLC/PLC_AAS/IOInterfaceMapping/Map_{i}/A" for i in range(100)]
    patterns = [item["pattern"] for item in suggested_wildcards("camtic", topics)]
    assert "HH7/Shell/RobotKinematicState/JointPosition/#" in patterns
    assert "PLC/PLC_AAS/IOInterfaceMapping/#" in patterns
    assert not any("Map_" in pattern for pattern in patterns)
    assert topic_matches("camtic/+/Shell/#", topics[0]) is True
    assert topic_matches("camtic/HH7/Shell", topics[0]) is False
    assert resolve_wildcard("+/HH7/#", "camtic") == "camtic/+/HH7/#"
    assert resolve_wildcard("camtic/#", "camtic") == ""
    try:
        resolve_wildcard("a/#/b", "camtic")
    except ValueError as exc:
        assert "#" in str(exc)
    else:
        raise AssertionError("invalid wildcard was accepted")


def _payload(package_id: str, path: str, value: object) -> bytes:
    return encode_payload(
        package_id=package_id,
        path=path,
        node_id="ns=1;s=node",
        value=value,
        status="Good",
        source_timestamp=WHEN,
        status_code=0,
    ).encode()


def test_mqtt_live_does_not_start_a_persistent_opcua_subscription():
    assert persistent_opcua_subscription(True, False) is True
    assert persistent_opcua_subscription(True, True) is False
    assert persistent_opcua_subscription(False, False) is False
    assert utility_connect_subscribes(False) is True
    assert utility_connect_subscribes(True) is False
