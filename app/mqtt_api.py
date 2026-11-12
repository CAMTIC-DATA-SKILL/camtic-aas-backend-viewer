"""유틸리티의 MQTT 구독 로그.

AAS 서버가 브로커를 구독 중일 때만 지금 값을 돌려준다.
토픽마다 마지막 값만 남기고, 와일드카드는 그 범위만 보여 준다.
``MQTT_ENABLED=false`` 이면 구독을 시작하지 않으므로 비활성임을 명시한다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from config import MqttConfig, settings

from .live_route import apply_live_route, route_fields
from .mqtt_feed import DISABLED_MESSAGE, MqttFeed
from .mqtt_messages import resolve_wildcard

router = APIRouter(prefix="/api/mqtt", tags=["mqtt"])


def subscriber_view(
    cfg: MqttConfig,
    feed: MqttFeed | None,
    limit: int = 200,
    wildcard: str = "",
) -> dict[str, Any]:
    topic = f"{cfg.topic_prefix}/#"
    try:
        pattern = resolve_wildcard(wildcard, cfg.topic_prefix)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    base = {
        "host": cfg.host,
        "port": cfg.port,
        "topic": topic,
        "clientId": cfg.client_id,
        "wildcard": pattern,
        "view": "latest",
    }
    route = route_fields()
    if not cfg.enabled or feed is None:
        return {
            **base,
            **route,
            "enabled": False,
            "connected": False,
            "subscriber": "disabled",
            "message": DISABLED_MESSAGE,
            "received": 0,
            "rejected": 0,
            "lastError": None,
            "matched": 0,
            "truncated": False,
            "entrySeq": 0,
            "branches": [],
            "entries": [],
        }
    entries, matched = feed.log_entries(limit, pattern)
    return {
        **base,
        **route,
        "enabled": True,
        "connected": feed.session.mqtt_connected,
        "subscriber": "enabled",
        "message": None,
        "received": feed.received,
        "rejected": feed.rejected,
        "lastError": feed.last_error,
        "matched": matched,
        "truncated": matched > len(entries),
        "entrySeq": max((int(item.get("seq") or 0) for item in entries), default=0),
        "branches": feed.package_branches(cfg.topic_prefix),
        "entries": entries,
    }


def _feed(request: Request) -> MqttFeed | None:
    feed = getattr(request.app.state, "mqtt_feed", None)
    return feed if isinstance(feed, MqttFeed) else None


@router.get("/logs")
async def mqtt_logs(
    request: Request,
    limit: int = Query(200, ge=1, le=300),
    wildcard: str = Query("", max_length=500),
) -> dict[str, Any]:
    return subscriber_view(settings.mqtt, _feed(request), limit, wildcard)


class RouteBody(BaseModel):
    enabled: bool


@router.post("/route")
async def mqtt_route(
    body: RouteBody,
    request: Request,
    wildcard: str = Query("", max_length=500),
) -> dict[str, Any]:
    """OPC-UA 직접 구독과 MQTT 구독을 바꾸고 ``.env`` 의 MQTT_ENABLED 에 기록한다."""
    try:
        await apply_live_route(request.app, body.enabled)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return subscriber_view(settings.mqtt, _feed(request), 200, wildcard)


@router.post("/logs/clear")
async def mqtt_logs_clear(
    request: Request,
    wildcard: str = Query("", max_length=500),
) -> dict[str, Any]:
    feed = _feed(request)
    if settings.mqtt.enabled and feed is not None:
        feed.clear_logs()
    return subscriber_view(settings.mqtt, feed, 200, wildcard)
