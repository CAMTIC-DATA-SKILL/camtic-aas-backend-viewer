"""OPC-UA 클라이언트 접속 스모크.

`.env` 의 OPCUA_ENDPOINT (또는 인자로 준 주소) 에 접속한 뒤
네임스페이스·Objects 하위 노드·Server.CurrentTime 을 확인하고 종료한다.

사용법 (레포 루트에서):
    python -m scripts.smoke_opcua_connect
    python -m scripts.smoke_opcua_connect opc.tcp://127.0.0.1:4840
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

from app.opc_ua import OpcUaClient, OpcUaClientError
from config import settings
from utils.log_setup import LogSetup

logger = LogSetup.get_logger(__name__)

SERVER_CURRENT_TIME = "i=2258"


async def run(endpoint: str) -> None:
    cfg = replace(settings.opcua, endpoint=endpoint)
    logger.info("OPC-UA 접속 시도: %s", cfg.endpoint)

    async with OpcUaClient(cfg) as client:
        namespaces = await client.namespaces()
        logger.info("네임스페이스 %d개: %s", len(namespaces), namespaces)

        children = await client.browse()
        logger.info("Objects 하위 노드 %d개", len(children))
        for item in children:
            logger.info("  - %s [%s] %s", item.browse_name, item.node_class, item.node_id)

        current_time = await client.read(SERVER_CURRENT_TIME)
        logger.info("Server.CurrentTime: %s", current_time)

    logger.info("OPC-UA 접속 스모크 성공: %s", cfg.endpoint)


def main() -> int:
    endpoint = sys.argv[1] if len(sys.argv) > 1 else settings.opcua.endpoint
    try:
        asyncio.run(run(endpoint))
    except OpcUaClientError as exc:
        logger.error("%s", exc)
        return 1
    except Exception:
        logger.exception("OPC-UA 접속 스모크 실패")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
