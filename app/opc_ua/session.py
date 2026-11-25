from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

from config import OpcUaConfig, settings
from utils.log_setup import LogSetup

from .client import BrowseItem, DataChange, OpcUaClient, OpcUaClientError
from .values import serialize_data_value, serialize_value

logger = LogSetup.get_logger(__name__)

# 한 발행에 노드 알림이 연속으로 온다. 마지막 알림 직후 한 번만 깨운다.
LIVE_COALESCE_S = 0.02


class OpcUaSession:
    """프로세스 전역 OPC-UA 접속과 화면용 최신값.

    상시 구독은 발행 프로세스가 맡고, 이 세션의 최신값은 MQTT 수신으로 채울 수 있다.
    browse·read·write 용 OPC-UA 연결은 유틸리티가 요청할 때만 연다.
    """

    def __init__(self, cfg: OpcUaConfig | None = None) -> None:
        self._default_cfg = cfg or settings.opcua
        self._lock = asyncio.Lock()
        self._client: OpcUaClient | None = None
        self._latest: dict[str, dict[str, Any]] = {}
        self._subscribed: list[str] = []
        self._desired_node_ids: list[str] = []
        self._catalog_server_id: Optional[str] = None
        self._subscribe_package_id: Optional[str] = None
        self._subscribe_package_ids: list[str] = []
        self._restore_task: asyncio.Task[None] | None = None
        self._reconnect_lock = asyncio.Lock()
        # 수신 알림을 _latest 외에 받을 곳 (예: TimescaleDB 기록). 콜백 안에서 오래 걸리면 안 된다.
        self._listeners: list[Callable[[DataChange, datetime], None]] = []
        self._live_queues: list[asyncio.Queue[None]] = []
        self._live_handle: asyncio.TimerHandle | None = None
        # opcua: 이 세션의 상시 구독이 live 캐시를 채운다. mqtt: 브로커 수신이 채운다.
        self._live_source = "opcua"
        self._mqtt_online = False

    def add_listener(self, listener: Callable[[DataChange, datetime], None]) -> None:
        self._listeners.append(listener)

    def watch_live(self) -> asyncio.Queue[None]:
        """구독 값이 캐시에 반영되면 한 번 깨우는 큐. 화면은 이 신호로 캐시를 읽는다."""
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._live_queues.append(queue)
        return queue

    def unwatch_live(self, queue: asyncio.Queue[None]) -> None:
        try:
            self._live_queues.remove(queue)
        except ValueError:
            return

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def is_transport_connected(self) -> bool:
        return self._client is not None and self._client.is_transport_connected

    @property
    def live_source(self) -> str:
        return self._live_source

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt_online

    @property
    def live_online(self) -> bool:
        """그래프 online. MQTT 경로에서는 브로커 연결, 아니면 OPC-UA 세션."""
        if self._live_source == "mqtt":
            return self._mqtt_online
        return self.is_connected

    def bind_mqtt_catalog(self, server_id: Optional[str], package_ids: list[str]) -> None:
        """MQTT로 받을 패키지를 등록한다. OPC-UA 세션은 열지 않는다."""
        self._live_source = "mqtt"
        if server_id:
            self._catalog_server_id = server_id
        self._remember_packages(package_ids, replace=True)

    def use_opcua_live(self) -> None:
        """실시간 값의 원본을 이 세션의 OPC-UA 구독으로 되돌린다."""
        self._live_source = "opcua"
        self.set_mqtt_online(False)

    async def handoff_to_mqtt(self) -> None:
        """상시 OPC-UA 구독을 놓고 실시간 원본을 MQTT로 둔다. 수신 캐시는 유지한다."""
        self._live_source = "mqtt"
        self._cancel_restore()
        async with self._lock:
            self._desired_node_ids = []
            self._subscribed = []
            client = self._client
            self._client = None
        if client is not None:
            await client.disconnect()

    def note_package(self, package_id: str) -> None:
        text = (package_id or "").strip()
        if text:
            self._remember_packages([text], replace=False)

    def set_mqtt_online(self, online: bool) -> None:
        changed = self._mqtt_online != online
        self._mqtt_online = online
        if changed and self._live_source == "mqtt":
            self._schedule_live_notify()

    async def apply_field_change(self, change: DataChange) -> None:
        """OPC-UA 콜백과 같은 형식으로 live 캐시와 기록 리스너를 갱신한다."""
        await self._on_data_change(change)

    @property
    def catalog_server_id(self) -> Optional[str]:
        return self._catalog_server_id

    @property
    def subscribe_package_ids(self) -> list[str]:
        if self._subscribe_package_ids:
            return list(self._subscribe_package_ids)
        single = (self._subscribe_package_id or "").strip()
        return [single] if single else []

    @property
    def subscribe_package_id(self) -> Optional[str]:
        ids = self.subscribe_package_ids
        return ids[0] if ids else None

    @property
    def subscribed_node_ids(self) -> list[str]:
        return list(self._subscribed)

    def status(self) -> dict[str, Any]:
        cfg = self._client.config if self._client is not None else self._default_cfg
        return {
            "connected": self.is_connected,
            "endpoint": cfg.endpoint,
            "clientName": cfg.client_name,
            "securityPolicy": cfg.security_policy,
            "catalogServerId": self._catalog_server_id,
            "subscribePackageId": self.subscribe_package_id,
            "subscribePackageIds": self.subscribe_package_ids,
            "subscribedNodeIds": list(self._subscribed),
            "latestCount": len(self._latest),
            "liveSource": self._live_source,
            "mqttConnected": self._mqtt_online,
        }

    async def connect(
        self,
        endpoint: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        security_policy: Optional[str] = None,
        security_mode: Optional[str] = None,
        catalog_server_id: Optional[str] = None,
    ) -> dict[str, Any]:
        cfg = self._overlay(
            endpoint=endpoint,
            username=username,
            password=password,
            security_policy=security_policy,
            security_mode=security_mode,
        )
        self._cancel_restore()
        restore = False
        async with self._lock:
            if catalog_server_id is not None:
                incoming = catalog_server_id or None
                # MQTT live 가 묶어 둔 카탈로그는 유틸리티의 빈 serverId 로 지우지 않는다.
                if incoming or self._live_source != "mqtt":
                    self._catalog_server_id = incoming
            if self._client is not None:
                if self._client.is_connected and self._client.config.endpoint == cfg.endpoint:
                    logger.info(
                        "OPC-UA 이미 연결됨: endpoint=%s server=%s",
                        cfg.endpoint,
                        self._catalog_server_id or "-",
                    )
                    restore = bool(self._desired_node_ids)
                else:
                    await self._client.disconnect()
                    self._release_opcua(clear_live=self._live_source != "mqtt")
                    client = OpcUaClient(cfg)
                    await self._attach_client(client)
                    restore = False
            else:
                client = OpcUaClient(cfg)
                await self._attach_client(client)
                restore = bool(self._desired_node_ids)

        if restore:
            await self.restore_subscription()
        return await self._connected_status()

    async def disconnect(self) -> dict[str, Any]:
        self._cancel_restore()
        async with self._lock:
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            clear_live = self._live_source != "mqtt"
            self._release_opcua(clear_live=clear_live)
            if clear_live:
                self._catalog_server_id = None
        return self.status()

    def require_client(self) -> OpcUaClient:
        if self._client is None or not self._client.is_connected:
            raise OpcUaClientError("OPC UA 서버에 연결되어 있지 않습니다.")
        return self._client

    async def browse(self, node_id: Optional[str] = None) -> list[BrowseItem]:
        return await self.require_client().browse(node_id)

    async def browse_variables(
        self,
        node_id: Optional[str] = None,
        *,
        max_nodes: int = 200,
        max_depth: int = 10,
    ) -> tuple[list[BrowseItem], bool]:
        return await self.require_client().browse_variables(
            node_id,
            max_nodes=max_nodes,
            max_depth=max_depth,
        )

    async def namespaces(self) -> list[str]:
        return await self.require_client().namespaces()

    async def read(self, node_ids: list[str]) -> list[dict[str, Any]]:
        values = await self.require_client().read_many_data_values(node_ids)
        return [serialize_data_value(node_id, dv) for node_id, dv in values.items()]

    async def write(self, node_id: str, value: Any) -> dict[str, Any]:
        await self.require_client().write(node_id, value)
        return {"nodeId": node_id, "written": True}

    async def subscribe(
        self,
        node_ids: list[str],
        package_id: Optional[str] = None,
        package_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            raise ValueError("구독할 노드 ID 가 없습니다.")
        async with self._lock:
            return await self._subscribe_locked(
                ids,
                self._owner_ids(package_id, package_ids),
                replace_packages=True,
            )

    async def unsubscribe(self) -> dict[str, Any]:
        async with self._lock:
            if self._client is not None:
                await self._client.unsubscribe_all()
            self._subscribed = []
            self._desired_node_ids = []
            if self._live_source != "mqtt":
                self._clear_packages()
        return {"subscribedNodeIds": []}

    async def restore_subscription(self) -> dict[str, Any] | None:
        ids = list(self._desired_node_ids)
        if not ids or not self.is_connected:
            return None
        return await self.subscribe(ids, package_ids=self.subscribe_package_ids)

    async def ensure_subscribed(
        self,
        node_ids: list[str],
        package_id: Optional[str] = None,
        package_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            raise ValueError("구독할 노드 ID 가 없습니다.")
        owners = self._owner_ids(package_id, package_ids)
        async with self._lock:
            if set(ids) <= set(self._subscribed):
                self._remember_packages(owners, replace=False)
                return self._subscribe_status()
            merged = list(dict.fromkeys([*self._subscribed, *ids]))
            kept = list(dict.fromkeys([*self.subscribe_package_ids, *(owners or [])]))
            return await self._subscribe_locked(merged, kept, replace_packages=True)

    def _owner_ids(
        self,
        package_id: Optional[str],
        package_ids: Optional[list[str]],
    ) -> Optional[list[str]]:
        if package_ids is None and package_id is None:
            return None
        return [*(package_ids or []), *([package_id] if package_id else [])]

    def _clear_packages(self) -> None:
        self._subscribe_package_ids = []
        self._subscribe_package_id = None

    def _remember_packages(self, package_ids: Optional[list[str]], *, replace: bool) -> None:
        if package_ids is None:
            return
        cleaned = [item.strip() for item in package_ids if item and item.strip()]
        if replace:
            self._subscribe_package_ids = list(dict.fromkeys(cleaned))
        else:
            self._subscribe_package_ids = list(dict.fromkeys([*self.subscribe_package_ids, *cleaned]))
        self._subscribe_package_id = self._subscribe_package_ids[0] if self._subscribe_package_ids else None

    def _subscribe_status(self) -> dict[str, Any]:
        return {
            "subscribedNodeIds": list(self._subscribed),
            "subscribePackageId": self.subscribe_package_id,
            "subscribePackageIds": self.subscribe_package_ids,
            "catalogServerId": self._catalog_server_id,
        }

    async def _subscribe_locked(
        self,
        ids: list[str],
        package_ids: Optional[list[str]] = None,
        *,
        replace_packages: bool = True,
    ) -> dict[str, Any]:
        client = self.require_client()
        keep = {node_id: value for node_id, value in self._latest.items() if node_id in ids}
        await client.unsubscribe_all()
        self._latest = keep
        self._subscribed = ids
        self._desired_node_ids = ids
        self._remember_packages(package_ids, replace=replace_packages)
        await client.subscribe(ids, self._on_data_change)
        logger.info(
            "OPC-UA 토픽 등록: server=%s packages=%s count=%d topics=%s",
            self._catalog_server_id or "-",
            self.subscribe_package_ids or "-",
            len(ids),
            ids,
        )
        return self._subscribe_status()

    def latest_values(self, node_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
        if node_ids:
            return [self._latest[n] for n in node_ids if n in self._latest]
        return list(self._latest.values())

    async def _connected_status(self) -> dict[str, Any]:
        payload = self.status()
        try:
            payload["namespaces"] = await self.require_client().namespaces()
        except Exception:
            logger.exception("네임스페이스 조회 실패")
            payload["namespaces"] = []
        return payload

    def remember_retained(self, change: DataChange) -> None:
        """브로커에 남아 있던 값을 live 캐시에 넣는다. 기록 리스너는 호출하지 않는다."""
        self._store_latest(change)
        self._schedule_live_notify()

    def _store_latest(self, change: DataChange) -> None:
        value = serialize_value(change.value)
        self._latest[change.node_id] = {
            "nodeId": change.node_id,
            "value": value,
            "status": change.status,
            "isGood": change.is_good,
            "sourceTimestamp": change.source_timestamp.isoformat() if change.source_timestamp else None,
            "serverTimestamp": change.server_timestamp.isoformat() if change.server_timestamp else None,
        }

    async def _on_data_change(self, change: DataChange) -> None:
        received_at = datetime.now(timezone.utc)
        self._store_latest(change)
        logger.debug(
            "OPC-UA 수신: node=%s value=%r status=%s",
            change.node_id,
            self._latest[change.node_id]["value"],
            change.status,
        )
        for listener in self._listeners:
            try:
                listener(change, received_at)
            except Exception:
                logger.exception("수신 리스너 오류 (node=%s)", change.node_id)
        self._schedule_live_notify()

    def _schedule_live_notify(self) -> None:
        if not self._live_queues:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._live_handle is not None:
            self._live_handle.cancel()
        self._live_handle = loop.call_later(LIVE_COALESCE_S, self._flush_live)

    def _flush_live(self) -> None:
        self._live_handle = None
        for queue in self._live_queues:
            if queue.full():
                continue
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                continue

    async def _attach_client(self, client: OpcUaClient) -> None:
        await client.connect()
        client.set_connection_lost_hook(self._on_client_lost)
        self._client = client

    def _on_client_lost(self, exc: Exception) -> None:
        if not self._desired_node_ids:
            return
        logger.info("연결 유실 후 구독 복구 예약: %r", exc)
        self._schedule_restore()

    def _schedule_restore(self) -> None:
        if self._restore_task is not None and not self._restore_task.done():
            return
        self._restore_task = asyncio.create_task(self._restore_after_lost())

    def _cancel_restore(self) -> None:
        task = self._restore_task
        self._restore_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _restore_after_lost(self) -> None:
        delay = 2.0
        for attempt in range(5):
            await asyncio.sleep(delay)
            if not self._desired_node_ids or self._client is None:
                return
            try:
                await self.restore_subscription()
                logger.info("구독 복구: %d개 노드", len(self._desired_node_ids))
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("구독 복구 실패 (%d회차)", attempt + 1, exc_info=True)
                delay = min(delay * 2, 16.0)

    def _release_opcua(self, *, clear_live: bool) -> None:
        """OPC-UA 구독만 해제한다. MQTT live 캐시와 패키지 등록은 clear_live 일 때만 지운다."""
        self._subscribed = []
        self._desired_node_ids = []
        if clear_live:
            self._latest = {}
            self._clear_packages()

    def _overlay(
        self,
        endpoint: Optional[str],
        username: Optional[str],
        password: Optional[str],
        security_policy: Optional[str],
        security_mode: Optional[str],
    ) -> OpcUaConfig:
        updates: dict[str, Any] = {}
        if endpoint:
            updates["endpoint"] = endpoint
        if username is not None:
            updates["username"] = username or None
        if password is not None:
            updates["password"] = password or None
        if security_policy:
            updates["security_policy"] = security_policy
        if security_mode:
            updates["security_mode"] = security_mode
        return replace(self._default_cfg, **updates) if updates else self._default_cfg
