from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from asyncua import Node, ua
from asyncua.common.subscription import DataChangeNotif

from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)


@dataclass(frozen=True)
class DataChange:
    node_id: str
    value: Any
    status: str
    is_good: bool
    source_timestamp: datetime | None
    server_timestamp: datetime | None


DataChangeCallback = Callable[[DataChange], Awaitable[None] | None]


class SubscriptionHandler:
    """asyncua 구독 알림을 `DataChange` 로 변환해 사용자 콜백으로 전달한다."""

    def __init__(self, callback: DataChangeCallback) -> None:
        self._callback = callback

    async def datachange_notification(self, node: Node, val: Any, data: DataChangeNotif) -> None:
        data_value = data.monitored_item.Value
        change = DataChange(
            node_id=node.nodeid.to_string(),
            value=val,
            status=data_value.StatusCode.name,
            is_good=data_value.StatusCode.is_good(),
            source_timestamp=data_value.SourceTimestamp,
            server_timestamp=data_value.ServerTimestamp,
        )
        try:
            result = self._callback(change)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("데이터 변경 콜백 처리 중 오류 (node=%s)", change.node_id)

    async def status_change_notification(self, status: ua.StatusChangeNotification) -> None:
        logger.warning("구독 상태 변경: %s", status.Status)
