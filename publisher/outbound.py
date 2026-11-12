"""값이 바뀐 토픽만 브로커에 올린다. 토픽마다 마지막 값은 retain 으로 남긴다."""

from __future__ import annotations

import asyncio

import aiomqtt

from config import MqttConfig
from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)

_RETRY_S = 2.0


class TopicOutbound:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, str]] = {}
        self._sent: dict[str, str] = {}
        self._wake = asyncio.Event()

    def reset_sent(self) -> None:
        """OPC-UA 세션을 다시 연 뒤 현재 값을 한 번은 올리게 한다."""
        self._sent.clear()

    def needs(self, topic: str, signature: str) -> bool:
        pending = self._pending.get(topic)
        if pending is not None and pending[0] == signature:
            return False
        if pending is None and self._sent.get(topic) == signature:
            return False
        return True

    def offer(self, topic: str, payload: str, signature: str) -> None:
        if not self.needs(topic, signature):
            return
        self._pending[topic] = (signature, payload)
        self._wake.set()

    def acknowledge(self, topic: str, signature: str) -> None:
        current = self._pending.get(topic)
        if current is not None and current[0] == signature:
            del self._pending[topic]
            self._sent[topic] = signature

    async def run(self, cfg: MqttConfig, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with _client(cfg) as client:
                    logger.info("MQTT 발행 연결: %s:%s", cfg.host, cfg.port)
                    while not stop.is_set():
                        if not self._pending:
                            self._wake.clear()
                            if self._pending:
                                continue
                            wait_stop = asyncio.create_task(stop.wait())
                            wait_wake = asyncio.create_task(self._wake.wait())
                            _done, pending = await asyncio.wait(
                                {wait_stop, wait_wake},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for task in pending:
                                task.cancel()
                            if stop.is_set():
                                return
                            continue
                        await self._flush(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("MQTT 발행 연결 실패: %s", exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=_RETRY_S)
                except asyncio.TimeoutError:
                    pass

    async def _flush(self, client: aiomqtt.Client) -> None:
        for topic, (signature, payload) in list(self._pending.items()):
            await client.publish(topic=topic, payload=payload, qos=1, retain=True)
            self.acknowledge(topic, signature)


def _client(cfg: MqttConfig) -> aiomqtt.Client:
    return aiomqtt.Client(
        cfg.host,
        port=cfg.port,
        username=cfg.username,
        password=cfg.password,
        identifier=cfg.publisher_client_id,
        keepalive=cfg.keepalive,
        timeout=10,
    )
