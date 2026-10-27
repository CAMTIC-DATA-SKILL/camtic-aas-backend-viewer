from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import Settings, settings
from utils.log_setup import LogSetup

from .api import router
from .live_api import router as live_router
from .live_record import LiveRecorder
from .opc_ua import OpcUaSession
from .opcua_api import connect_from_catalog, router as opcua_router
from .opcua_catalog import load_catalog
from .opcua_export import generate_opcua_artifacts, list_artifacts
from .repository import AasxRepository

logger = LogSetup.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


async def _startup_opcua(opcua: OpcUaSession, repository: AasxRepository) -> None:
    try:
        catalog = load_catalog()
    except Exception:
        logger.exception("OPC-UA 카탈로그 로드 실패")
        return
    server = catalog.default_server()
    if server is None:
        logger.warning("OPC-UA 카탈로그에 서버가 없습니다.")
        return
    logger.info(
        "OPC-UA 카탈로그 접속 시도: server=%s endpoint=%s aasId=%s",
        server.id,
        server.endpoint,
        server.aas_id or "-",
    )
    try:
        status = await connect_from_catalog(
            opcua,
            repository,
            server_id=server.id,
            use_default_server=True,
        )
    except Exception as exc:
        logger.warning("OPC-UA 자동 접속 실패: %s", exc)
        return
    subscribed = len(status.get("subscribedNodeIds") or [])
    if status.get("subscribeError"):
        logger.warning("OPC-UA 매핑 구독 실패: %s", status["subscribeError"])
        return
    logger.info(
        "OPC-UA 토픽 등록: endpoint=%s server=%s aasId=%s count=%d",
        status.get("endpoint"),
        status.get("catalogServerId") or server.id,
        server.aas_id or "-",
        subscribed,
    )


def _ensure_artifacts(repository: AasxRepository) -> None:
    for package in repository.list():
        if package.error:
            continue
        artifact_dir = repository.artifact_dir(package.id)
        if list_artifacts(artifact_dir):
            continue
        try:
            generate_opcua_artifacts(package, artifact_dir)
        except Exception:
            logger.exception("Failed to generate OPC-UA artifacts for %s", package.id)


def create_app(app_settings: Settings = settings) -> FastAPI:
    repository = AasxRepository(app_settings.aasx_data_dir, app_settings.artifacts_dir)
    opcua = OpcUaSession(app_settings.opcua)
    recorder = LiveRecorder(app_settings.record_dir, app_settings.record_interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Loading AASX packages from %s", app_settings.aasx_data_dir)
        logger.info("OPC-UA artifacts directory: %s", app_settings.artifacts_dir)
        repository.reload()
        _ensure_artifacts(repository)
        await _startup_opcua(opcua, repository)
        yield
        await recorder.stop()
        await opcua.disconnect()

    app = FastAPI(
        title="CAMTIC AAS Data Platform API",
        description="Eclipse BaSyx Python SDK로 AASX를 파싱·관리하고 OPC-UA 산출물 생성·현장 서버 조회를 제공하는 API",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.opcua = opcua
    app.state.recorder = recorder
    app.state.collector_url = app_settings.collector_url or ""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(opcua_router)
    app.include_router(live_router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="viewer")
    return app


app = create_app()
