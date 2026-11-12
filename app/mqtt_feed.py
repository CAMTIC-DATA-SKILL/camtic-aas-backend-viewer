"""AAS 서버의 MQTT 구독.

브로커에 남아 있는 마지막 값과 이후 변경을 live 캐시에 넣는다.
캐시가 비면 발행 목록의 노드만 브로커에서 다시 읽어 채운다.
TimescaleDB 기록은 기존 OPC-UA 수신 리스너와 같은 콜백으로 들어간다.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import Any

import aiomqtt

from config import MqttConfig, settings
from utils.log_setup import LogSetup

from .mqtt_messages import (
    FieldUpdate,
    decode_payload,
    json_safe,
    branch_choices,
    load_publish_plan,
    topic_matches,
)
from .opc_ua.client import DataChange
from .opc_ua.session import OpcUaSession
from .opcua_catalog import load_catalog

logger = LogSetup.get_logger(__name__)

_RETRY_S = 2.0
_LOG_LIMIT = 300
_TOPIC_LIMIT = 2000
_PAYLOAD_LIMIT = 4000
_RETAIN_DEADLINE_S = 1.5
_RETAIN_QUIET_S = 0.4
_ABSENT_TTL_S = 60.0
DISABLED_MESSAGE = (
    "지금은 OPC-UA를 직접 구독합니다. MQTT 브로커로 바꾸면 이 서버의 OPC-UA 상시 구독이 멈추고 .env 의 MQTT_ENABLED 가 true 가 됩니다."
)


class MqttFeed:
    def __init__(self, cfg: MqttConfig, session: OpcUaSession) -> None:
        self.cfg = cfg
        self.session = session
        self.received = 0
        self.rejected = 0
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._seq = 0
        self._topics: dict[str, dict[str, Any]] = {}
        self._published: dict[str, str] = {}
        self._plan_loaded = False
        self._absent_until: dict[str, float] = {}
        self._broker_retry_at = 0.0
        self._fill_lock = asyncio.Lock()

    def bind_catalog(self) -> int:
        """카탈로그와 engineering.csv 로 화면이 읽을 패키지를 등록한다. 네트워크는 열지 않는다."""
        server_id = None
        namespace = None
        try:
            server = load_catalog().default_server()
        except Exception:
            logger.exception("MQTT 구독용 카탈로그 로드 실패")
            server = None
        if server is not None:
            server_id = server.id
            namespace = server.node_id_namespace
        try:
            plan = load_publish_plan(
                settings.storage.packages_dir,
                settings.storage.package_opcua_dir,
                namespace,
                self.cfg.topic_prefix,
                aas_ids=server.aas_ids if server is not None else (),
            )
        except Exception:
            logger.exception("engineering.csv 발행 목록을 읽지 못했습니다.")
            plan = []
        package_ids = list(dict.fromkeys(point.package_id for point in plan))
        self._published = {point.node_id: point.topic for point in plan}
        self._plan_loaded = True
        self.session.bind_mqtt_catalog(server_id, package_ids)
        logger.info(
            "MQTT 실시간 대상: server=%s packages=%s variables=%d",
            server_id or "-",
            package_ids or "-",
            len(plan),
        )
        return len(plan)

    async def handle_message(self, payload: bytes | str, topic: str = "") -> None:
        update = decode_payload(payload)
        if update is None:
            self.rejected += 1
            self._record(topic, payload, accepted=False, update=None)
            preview = payload if isinstance(payload, str) else bytes(payload[:120])
            logger.warning("MQTT 메시지 무시: %r", preview)
            return
        self.session.note_package(update.package_id)
        await self.session.apply_field_change(_change(update))
        self._absent_until.pop(update.node_id, None)
        self.received += 1
        self._record(topic, payload, accepted=True, update=update)

    def nodes_missing_from_cache(self) -> set[str]:
        """발행 목록에는 있으나 live 캐시에 없고, 방금 브로커에 없다고 확인한 노드는 뺀다."""
        if not self._plan_loaded:
            self.bind_catalog()
        cached = {item.get("nodeId") for item in self.session.latest_values()}
        now = time.monotonic()
        missing: set[str] = set()
        for node_id in self._published:
            if node_id in cached:
                self._absent_until.pop(node_id, None)
                continue
            if self._absent_until.get(node_id, 0) > now:
                continue
            missing.add(node_id)
        return missing

    async def fill_missing_from_broker(self) -> int:
        """캐시에 없는 발행 노드를 브로커의 마지막 값으로 채운다."""
        if time.monotonic() < self._broker_retry_at or not self.nodes_missing_from_cache():
            return 0
        async with self._fill_lock:
            missing = self.nodes_missing_from_cache()
            if not missing:
                return 0
            try:
                found = await self._fetch_retained(missing)
            except Exception:
                self._broker_retry_at = time.monotonic() + _ABSENT_TTL_S
                logger.warning("MQTT 브로커에서 남은 값을 읽지 못했습니다.", exc_info=True)
                return 0
            now = time.monotonic()
            for node_id in missing - found:
                self._absent_until[node_id] = now + _ABSENT_TTL_S
            if found:
                logger.info("MQTT 브로커에서 live 캐시를 채움: %d개", len(found))
            return len(found)

    async def _fetch_retained(self, missing: set[str]) -> set[str]:
        found: set[str] = set()
        queue: asyncio.Queue[aiomqtt.Message] = asyncio.Queue()

        async def consume(client: aiomqtt.Client) -> None:
            async for message in client.messages:
                queue.put_nowait(message)

        identifier = f"{self.cfg.client_id}-retain"
        async with _client(self.cfg, identifier) as client:
            await client.subscribe(f"{self.cfg.topic_prefix}/#", qos=1)
            task = asyncio.create_task(consume(client))
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + _RETAIN_DEADLINE_S
                quiet_until = loop.time() + _RETAIN_QUIET_S
                while found != missing and loop.time() < deadline:
                    timeout = min(quiet_until, deadline) - loop.time()
                    if timeout <= 0:
                        break
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout)
                    except asyncio.TimeoutError:
                        break
                    if not message.retain:
                        continue
                    quiet_until = min(deadline, loop.time() + _RETAIN_QUIET_S)
                    update = decode_payload(message.payload)
                    if update is None or update.node_id not in missing or update.node_id in found:
                        continue
                    self.session.note_package(update.package_id)
                    self.session.remember_retained(_change(update))
                    found.add(update.node_id)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        return found

    def clear_logs(self) -> None:
        self._topics.clear()

    def log_entries(self, limit: int, wildcard: str = "") -> tuple[list[dict[str, Any]], int]:
        """토픽마다 마지막 값만 경로 순으로 돌려준다. 이전 수신은 남기지 않는다."""
        size = max(1, min(int(limit), _LOG_LIMIT))
        matched = [
            entry
            for topic, entry in self._topics.items()
            if topic and (not wildcard or topic_matches(wildcard, topic))
        ]
        matched.sort(key=lambda entry: str(entry.get("topic") or ""))
        return matched[:size], len(matched)

    def package_branches(self, prefix: str) -> list[dict[str, Any]]:
        topics = [topic for topic in self._topics if topic]
        return branch_choices(prefix, topics)

    def _record(
        self,
        topic: str,
        payload: bytes | str,
        *,
        accepted: bool,
        update: FieldUpdate | None,
    ) -> None:
        self._seq += 1
        text = payload if isinstance(payload, str) else bytes(payload).decode("utf-8", "replace")
        if len(text) > _PAYLOAD_LIMIT:
            text = text[:_PAYLOAD_LIMIT] + "…"
        entry = {
            "seq": self._seq,
            "at": datetime.now(timezone.utc).isoformat(),
            "topic": topic,
            "payload": text,
            "accepted": accepted,
            "packageId": update.package_id if update else None,
            "path": update.path if update else None,
            "nodeId": update.node_id if update else None,
            "value": json_safe(update.value) if update else None,
            "status": update.status if update else None,
            "sourceTimestamp": (
                update.source_timestamp.isoformat() if update and update.source_timestamp else None
            ),
        }
        if not topic:
            return
        self._topics[topic] = entry
        self._drop_oldest_topic(topic)

    def _drop_oldest_topic(self, keep: str) -> None:
        overflow = len(self._topics) - _TOPIC_LIMIT
        if overflow <= 0:
            return
        oldest = sorted(self._topics, key=lambda name: int(self._topics[name].get("seq") or 0))
        for name in oldest:
            if overflow <= 0 or name == keep:
                continue
            del self._topics[name]
            overflow -= 1

    def status(self) -> dict[str, Any]:
        return {
            "liveSource": self.session.live_source,
            "mqttConnected": self.session.mqtt_connected,
            "mqttMessages": self.received,
            "mqttRejected": self.rejected,
            "mqttLastError": self.last_error,
        }

    async def start(self) -> None:
        if self._task is not None:
            return
        self.bind_catalog()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="mqtt-feed")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.session.set_mqtt_online(False)

    async def _run(self) -> None:
        topic = f"{self.cfg.topic_prefix}/#"
        while not self._stop.is_set():
            try:
                async with _client(self.cfg) as client:
                    await client.subscribe(topic=topic, qos=1)
                    self.last_error = None
                    self.session.set_mqtt_online(True)
                    logger.info(
                        "MQTT 구독 연결: %s:%s topic=%s",
                        self.cfg.host,
                        self.cfg.port,
                        topic,
                    )
                    async for message in client.messages:
                        if self._stop.is_set():
                            break
                        payload = message.payload
                        topic = str(message.topic)
                        if isinstance(payload, str):
                            await self.handle_message(payload, topic)
                        else:
                            await self.handle_message(bytes(payload), topic)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.warning("MQTT 구독 연결 실패: %s", exc)
            finally:
                self.session.set_mqtt_online(False)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_RETRY_S)
            except asyncio.TimeoutError:
                pass


def _client(cfg: MqttConfig, identifier: str | None = None) -> aiomqtt.Client:
    return aiomqtt.Client(
        cfg.host,
        port=cfg.port,
        username=cfg.username,
        password=cfg.password,
        identifier=identifier or cfg.client_id,
        keepalive=cfg.keepalive,
        timeout=10,
    )


def _change(update: FieldUpdate) -> DataChange:
    return DataChange(
        node_id=update.node_id,
        value=update.value,
        status=update.status,
        is_good=update.status.startswith("Good"),
        source_timestamp=update.source_timestamp,
        server_timestamp=None,
        status_code=update.status_code,
    )
