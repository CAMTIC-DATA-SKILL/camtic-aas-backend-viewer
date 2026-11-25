"""화면 실시간 경로를 OPC-UA 직접 구독과 MQTT 구독 사이에서 바꾼다."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI

from config import set_mqtt_enabled, settings

from .mqtt_feed import MqttFeed
from .opc_ua.session import OpcUaSession
from .repository import AasxRepository

_lock = asyncio.Lock()


async def apply_live_route(app: FastAPI, enabled: bool) -> None:
    """``.env`` 의 ``MQTT_ENABLED`` 와 이 프로세스의 실시간 구독을 같이 바꾼다."""
    async with _lock:
        previous = settings.mqtt.enabled
        if previous == enabled:
            return
        set_mqtt_enabled(enabled)
        try:
            if enabled:
                await _enable_mqtt(app)
            else:
                await _enable_opcua(app)
        except Exception:
            set_mqtt_enabled(previous)
            if previous:
                await _enable_mqtt(app)
            else:
                await _enable_opcua(app)
            raise


async def _enable_mqtt(app: FastAPI) -> None:
    opcua: OpcUaSession = app.state.opcua
    feed = getattr(app.state, "mqtt_feed", None)
    if not isinstance(feed, MqttFeed):
        feed = MqttFeed(settings.mqtt, opcua)
        app.state.mqtt_feed = feed
    feed.bind_catalog()
    await opcua.handoff_to_mqtt()
    await feed.start()


async def _enable_opcua(app: FastAPI) -> None:
    from .main import _startup_opcua, persistent_opcua_subscription

    opcua: OpcUaSession = app.state.opcua
    repository: AasxRepository = app.state.repository
    feed = getattr(app.state, "mqtt_feed", None)
    if isinstance(feed, MqttFeed):
        await feed.stop()
    opcua.use_opcua_live()
    if persistent_opcua_subscription(settings.manufacturing.auto_connect_opcua, False):
        await _startup_opcua(opcua, repository)


def route_fields() -> dict[str, Any]:
    return {
        "liveSource": "mqtt" if settings.mqtt.enabled else "opcua",
        "autoConnectOpcua": settings.manufacturing.auto_connect_opcua,
    }
