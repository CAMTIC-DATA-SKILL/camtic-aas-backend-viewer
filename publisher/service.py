"""카탈로그와 engineering.csv 를 읽어 Variable 을 구독하고 MQTT로 발행한다."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from config import OpcUaConfig, settings
from utils.log_setup import LogSetup

from app.mqtt_messages import PublishPoint, encode_payload, index_by_node, load_publish_plan, value_signature
from app.opc_ua.client import DataChange, OpcUaClient
from app.opc_ua.values import serialize_value
from app.opcua_catalog import CatalogServer, load_catalog

from .outbound import TopicOutbound

logger = LogSetup.get_logger(__name__)

_SUBSCRIBE_CHUNK = 200
_CATALOG_WAIT_S = 10.0
_PLAN_WAIT_S = 30.0
_MISSING_RETRY_S = 15.0


def publisher_opcua_config() -> tuple[CatalogServer, OpcUaConfig]:
    server = load_catalog().default_server()
    if server is None:
        raise LookupError("OPC-UA 카탈로그에 기본 서버가 없습니다.")
    cfg = replace(
        settings.opcua,
        endpoint=server.endpoint,
        security_policy=server.security_policy,
        security_mode=server.security_mode,
        client_name="camtic_aas_publisher",
        application_uri="urn:camtic:aas:opcua:publisher",
        server_id=server.id,
        connect_max_retries=1,
    )
    return server, cfg


def queue_change(
    outbound: TopicOutbound,
    index: dict[str, list[PublishPoint]],
    change: DataChange,
) -> None:
    points = index.get(change.node_id)
    if not points:
        return
    value = serialize_value(change.value)
    signature = value_signature(value, change.status)
    for point in points:
        if not outbound.needs(point.topic, signature):
            continue
        payload = encode_payload(
            package_id=point.package_id,
            path=point.path,
            node_id=point.node_id,
            value=value,
            status=change.status,
            source_timestamp=change.source_timestamp,
            status_code=change.status_code,
        )
        outbound.offer(point.topic, payload, signature)


async def run_publisher(stop: asyncio.Event) -> None:
    if settings.mqtt.client_id == settings.mqtt.publisher_client_id:
        logger.warning("MQTT 클라이언트 ID 가 구독 측과 같습니다. 브로커가 한쪽 연결을 끊습니다.")
    outbound = TopicOutbound()
    mqtt_task = asyncio.create_task(outbound.run(settings.mqtt, stop), name="mqtt-publisher")
    try:
        while not stop.is_set():
            try:
                await _serve_opcua(outbound, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("발행 프로세스 OPC-UA 실패: %s", exc)
                await _sleep(stop, _CATALOG_WAIT_S)
    finally:
        stop.set()
        mqtt_task.cancel()
        try:
            await mqtt_task
        except asyncio.CancelledError:
            pass


async def _serve_opcua(outbound: TopicOutbound, stop: asyncio.Event) -> None:
    if stop.is_set():
        return
    try:
        server, cfg = publisher_opcua_config()
    except Exception as exc:
        logger.warning("OPC-UA 카탈로그를 읽지 못했습니다: %s", exc)
        await _sleep(stop, _CATALOG_WAIT_S)
        return
    plan = load_publish_plan(
        settings.storage.packages_dir,
        settings.storage.package_opcua_dir,
        server.node_id_namespace,
        settings.mqtt.topic_prefix,
        aas_ids=server.aas_ids,
    )
    if not plan:
        logger.warning("발행할 Variable 이 없습니다. engineering.csv 를 기다립니다.")
        await _sleep(stop, _PLAN_WAIT_S)
        return

    outbound.reset_sent()
    index = index_by_node(plan)
    node_ids = list(dict.fromkeys(point.node_id for point in plan))
    client = OpcUaClient(cfg)

    async def on_change(change: DataChange) -> None:
        queue_change(outbound, index, change)

    try:
        await client.connect()
        loop = asyncio.get_running_loop()
        client.set_connection_lost_hook(lambda _exc: loop.call_soon_threadsafe(outbound.reset_sent))
        missing: list[str] = []
        for start in range(0, len(node_ids), _SUBSCRIBE_CHUNK):
            _subscription, failed = await client.subscribe(
                node_ids[start : start + _SUBSCRIBE_CHUNK], on_change
            )
            missing.extend(failed)
        logger.info(
            "MQTT 발행 시작: server=%s endpoint=%s variables=%d missing=%d",
            server.id,
            server.endpoint,
            len(plan),
            len(missing),
        )
        retry = asyncio.create_task(
            _retry_missing(client, on_change, missing, stop),
            name="opcua-missing-retry",
        )
        try:
            await _wait_until_session_ends(client, stop)
        finally:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
    finally:
        await client.disconnect()


async def _retry_missing(
    client: OpcUaClient,
    on_change,
    missing: list[str],
    stop: asyncio.Event,
) -> None:
    """구독 시점에 없던 노드가 나중에 생기면 다시 구독한다."""
    pending = list(dict.fromkeys(missing))
    while pending and not stop.is_set():
        await _sleep(stop, _MISSING_RETRY_S)
        if stop.is_set() or not client.is_transport_connected:
            continue
        still: list[str] = []
        for start in range(0, len(pending), _SUBSCRIBE_CHUNK):
            _subscription, failed = await client.subscribe(
                pending[start : start + _SUBSCRIBE_CHUNK],
                on_change,
                quiet=True,
            )
            still.extend(failed)
        gained = len(pending) - len(still)
        if gained:
            logger.info("나중에 생긴 노드 구독: %d개, 아직 없음 %d개", gained, len(still))
        pending = still


async def _wait_until_session_ends(client: OpcUaClient, stop: asyncio.Event) -> None:
    """auto_reconnect 가 복구하지 못하면 세션을 닫고 바깥 루프가 다시 접속한다."""
    loop = asyncio.get_running_loop()
    down_since: float | None = None
    while not stop.is_set():
        if client.is_transport_connected:
            down_since = None
        else:
            now = loop.time()
            down_since = now if down_since is None else down_since
            if now - down_since >= 60:
                logger.warning("OPC-UA 연결이 60초 동안 복구되지 않아 세션을 다시 엽니다.")
                return
        await _sleep(stop, 2)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
