from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from asyncua import Client, Node, ua
from asyncua.common.subscription import Subscription

from config import OpcUaConfig, settings
from utils.log_setup import LogSetup

from .handler import DataChangeCallback, SubscriptionHandler

logger = LogSetup.get_logger(__name__)

ConnectionLostHook = Callable[[Exception], Awaitable[None] | None]


class OpcUaClientError(Exception):
    pass


@dataclass(frozen=True)
class BrowseItem:
    node_id: str
    browse_name: str
    display_name: str
    node_class: str


class OpcUaClient:
    """
    asyncua 기반 OPC UA 클라이언트 래퍼.

    middleware_gateway_opc_ua 와 같은 형식이다.
    - 최초 접속 실패 시 설정된 간격/횟수만큼 재시도
    - 접속 이후 연결 유실은 asyncua auto_reconnect 로 복구 (구독 포함)

        async with OpcUaClient() as client:
            value = await client.read("ns=2;i=2")
    """

    def __init__(self, cfg: OpcUaConfig | None = None) -> None:
        self._cfg = cfg or settings.opcua
        self._client: Client | None = None
        self._subscriptions: list[Subscription] = []
        self._connection_lost_hook: ConnectionLostHook | None = None

    def set_connection_lost_hook(self, hook: ConnectionLostHook | None) -> None:
        self._connection_lost_hook = hook

    async def __aenter__(self) -> OpcUaClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    @property
    def config(self) -> OpcUaConfig:
        return self._cfg

    @property
    def client(self) -> Client:
        if self._client is None:
            raise OpcUaClientError("OPC UA 서버에 연결되어 있지 않습니다.")
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    async def _build_client(self) -> Client:
        cfg = self._cfg
        client = Client(
            url=cfg.endpoint,
            timeout=cfg.timeout,
            watchdog_intervall=cfg.watchdog_interval,
            auto_reconnect=cfg.auto_reconnect,
            reconnect_max_delay=cfg.reconnect_max_delay,
        )
        client.name = cfg.client_name
        client.description = cfg.client_name
        client.application_uri = cfg.application_uri
        client.session_timeout = cfg.session_timeout_ms
        client.connection_lost_callback = self._on_connection_lost

        if cfg.username:
            client.set_user(cfg.username)
        if cfg.password:
            client.set_password(cfg.password)

        if cfg.use_security:
            parts = [cfg.security_policy, cfg.security_mode, str(cfg.client_cert), str(cfg.client_key)]
            if cfg.server_cert:
                parts.append(str(cfg.server_cert))
            await client.set_security_string(",".join(parts))

        return client

    async def connect(self) -> None:
        if self._client is not None:
            return

        cfg = self._cfg
        attempt = 0
        while True:
            attempt += 1
            client = await self._build_client()
            try:
                logger.info("OPC UA 서버 접속 시도 (%d회차): %s", attempt, cfg.endpoint)
                await client.connect()
            except (OSError, asyncio.TimeoutError, ua.UaError) as e:
                if 0 < cfg.connect_max_retries <= attempt:
                    raise OpcUaClientError(
                        f"OPC UA 서버 접속 실패 ({attempt}회 시도): {cfg.endpoint}"
                    ) from e
                logger.warning(
                    "OPC UA 서버 접속 실패: %r - %.1f초 후 재시도", e, cfg.connect_retry_interval
                )
                await asyncio.sleep(cfg.connect_retry_interval)
                continue

            self._client = client
            logger.info("OPC UA 서버 접속 성공: %s", cfg.endpoint)
            return

    async def disconnect(self) -> None:
        if self._client is None:
            return

        await self.unsubscribe_all()
        try:
            await self._client.disconnect()
        except Exception:
            logger.exception("OPC UA 서버 연결 종료 중 오류")
        finally:
            self._client = None
            logger.info("OPC UA 서버 연결 종료: %s", self._cfg.endpoint)

    async def _on_connection_lost(self, exc: Exception) -> None:
        if self._cfg.auto_reconnect:
            logger.warning("OPC UA 연결 유실: %r - 자동 재연결 시도", exc)
        else:
            logger.error("OPC UA 연결 유실: %r", exc)
        hook = self._connection_lost_hook
        if hook is None:
            return
        try:
            result = hook(exc)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("연결 유실 훅 처리 중 오류")

    def get_node(self, node_id: str | Node) -> Node:
        return self.client.get_node(node_id)

    async def namespaces(self) -> list[str]:
        return await self.client.get_namespace_array()

    async def read(self, node_id: str) -> Any:
        return await self.get_node(node_id).read_value()

    async def read_data_value(self, node_id: str) -> ua.DataValue:
        return await self.get_node(node_id).read_data_value(raise_on_bad_status=False)

    async def read_many(self, node_ids: Iterable[str]) -> dict[str, Any]:
        ids = list(node_ids)
        if not ids:
            return {}
        values = await self.client.read_values([self.get_node(n) for n in ids])
        return dict(zip(ids, values))

    async def read_many_data_values(self, node_ids: Iterable[str]) -> dict[str, ua.DataValue]:
        ids = list(node_ids)
        if not ids:
            return {}
        nodes = [self.get_node(n) for n in ids]
        values = await asyncio.gather(
            *[node.read_data_value(raise_on_bad_status=False) for node in nodes]
        )
        return dict(zip(ids, values))

    async def write(
        self,
        node_id: str,
        value: Any,
        variant_type: ua.VariantType | None = None,
    ) -> None:
        """variant_type 미지정 시 노드의 DataType 을 조회해 타입을 맞춰 쓴다."""
        node = self.get_node(node_id)
        if variant_type is None:
            variant_type = await node.read_data_type_as_variant_type()
        await node.write_value(ua.DataValue(ua.Variant(value, variant_type)))

    async def browse(self, node_id: str | None = None) -> list[BrowseItem]:
        """node_id 미지정 시 Objects 폴더의 하위 노드를 조회한다."""
        node = self.client.nodes.objects if node_id is None else self.get_node(node_id)
        descriptions = await node.get_children_descriptions()
        return [
            BrowseItem(
                node_id=desc.NodeId.to_string(),
                browse_name=desc.BrowseName.to_string(),
                display_name=(desc.DisplayName.Text or desc.BrowseName.Name or ""),
                node_class=desc.NodeClass.name,
            )
            for desc in descriptions
        ]

    async def subscribe(
        self,
        node_ids: Iterable[str],
        callback: DataChangeCallback,
        period_ms: int | None = None,
        sampling_interval_ms: int | None = None,
    ) -> Subscription:
        ids = list(node_ids)
        if not ids:
            raise ValueError("구독할 노드 ID 가 없습니다.")

        period = period_ms if period_ms is not None else self._cfg.subscription_period_ms
        sampling = (
            sampling_interval_ms if sampling_interval_ms is not None else self._cfg.sampling_interval_ms
        )

        subscription = await self.client.create_subscription(period, SubscriptionHandler(callback))
        results = await subscription.subscribe_data_change(
            [self.get_node(n) for n in ids],
            sampling_interval=sampling,
        )

        failed = 0
        for node_id, result in zip(ids, results):
            if isinstance(result, ua.StatusCode):
                failed += 1
                logger.error("노드 구독 실패: %s (%s)", node_id, result.name)

        self._subscriptions.append(subscription)
        logger.info(
            "구독 생성: %d/%d개 노드 (period=%dms, sampling=%dms)",
            len(ids) - failed,
            len(ids),
            period,
            sampling,
        )
        return subscription

    async def unsubscribe_all(self) -> None:
        while self._subscriptions:
            subscription = self._subscriptions.pop()
            try:
                await subscription.delete()
            except Exception:
                logger.warning("구독 삭제 실패 (id=%s)", subscription.subscription_id, exc_info=True)
