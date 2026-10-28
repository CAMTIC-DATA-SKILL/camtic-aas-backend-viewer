from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Optional

from config import OpcUaConfig, settings
from utils.log_setup import LogSetup

from .client import BrowseItem, DataChange, OpcUaClient, OpcUaClientError
from .values import serialize_data_value, serialize_value

logger = LogSetup.get_logger(__name__)


class OpcUaSession:
    """프로세스 전역 OPC-UA 접속. HTTP 요청 사이에 연결을 유지한다."""

    def __init__(self, cfg: OpcUaConfig | None = None) -> None:
        self._default_cfg = cfg or settings.opcua
        self._lock = asyncio.Lock()
        self._client: OpcUaClient | None = None
        self._latest: dict[str, dict[str, Any]] = {}
        self._subscribed: list[str] = []
        self._desired_node_ids: list[str] = []
        self._catalog_server_id: Optional[str] = None
        self._subscribe_package_id: Optional[str] = None
        self._restore_task: asyncio.Task[None] | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def catalog_server_id(self) -> Optional[str]:
        return self._catalog_server_id

    @property
    def subscribe_package_id(self) -> Optional[str]:
        return self._subscribe_package_id

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
            "subscribePackageId": self._subscribe_package_id,
            "subscribedNodeIds": list(self._subscribed),
            "latestCount": len(self._latest),
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
                self._catalog_server_id = catalog_server_id or None
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
                    self._reset_cache()
                    self._desired_node_ids = []
                    self._subscribe_package_id = None
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
            self._reset_cache()
            self._desired_node_ids = []
            self._catalog_server_id = None
            self._subscribe_package_id = None
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
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            raise ValueError("구독할 노드 ID 가 없습니다.")
        async with self._lock:
            client = self.require_client()
            await client.unsubscribe_all()
            self._latest = {}
            self._subscribed = ids
            self._desired_node_ids = ids
            if package_id is not None:
                self._subscribe_package_id = package_id or None
            await client.subscribe(ids, self._on_data_change)
        logger.info(
            "OPC-UA 토픽 등록: server=%s package=%s count=%d topics=%s",
            self._catalog_server_id or "-",
            self._subscribe_package_id or "-",
            len(ids),
            ids,
        )
        return {
            "subscribedNodeIds": ids,
            "subscribePackageId": self._subscribe_package_id,
            "catalogServerId": self._catalog_server_id,
        }

    async def unsubscribe(self) -> dict[str, Any]:
        async with self._lock:
            if self._client is not None:
                await self._client.unsubscribe_all()
            self._subscribed = []
            self._desired_node_ids = []
            self._subscribe_package_id = None
        return {"subscribedNodeIds": []}

    async def restore_subscription(self) -> dict[str, Any] | None:
        ids = list(self._desired_node_ids)
        if not ids or not self.is_connected:
            return None
        return await self.subscribe(ids, package_id=self._subscribe_package_id)

    async def ensure_subscribed(
        self,
        node_ids: list[str],
        package_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            raise ValueError("구독할 노드 ID 가 없습니다.")
        if set(ids) <= set(self._subscribed):
            if package_id is not None:
                self._subscribe_package_id = package_id or None
            return {
                "subscribedNodeIds": list(self._subscribed),
                "subscribePackageId": self._subscribe_package_id,
                "catalogServerId": self._catalog_server_id,
            }
        return await self.subscribe(ids, package_id=package_id)

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

    async def _on_data_change(self, change: DataChange) -> None:
        value = serialize_value(change.value)
        self._latest[change.node_id] = {
            "nodeId": change.node_id,
            "value": value,
            "status": change.status,
            "isGood": change.is_good,
            "sourceTimestamp": change.source_timestamp.isoformat() if change.source_timestamp else None,
            "serverTimestamp": change.server_timestamp.isoformat() if change.server_timestamp else None,
        }
        logger.debug(
            "OPC-UA 수신: node=%s value=%r status=%s",
            change.node_id,
            value,
            change.status,
        )

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

    def _reset_cache(self) -> None:
        self._latest = {}
        self._subscribed = []

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
