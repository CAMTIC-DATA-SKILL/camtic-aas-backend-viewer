from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import Settings, settings
from utils.log_setup import LogSetup

from .api import router
from .camera_proxy import router as camera_router
from .manufacturing.alarms import AlarmEngine
from .manufacturing.api import ManufacturingRuntime, router as manufacturing_router
from .manufacturing.db import create_engine, create_sessionmaker
from .manufacturing.ingest import NodeCache
from .manufacturing.package_sync import PackageSync, SyncContext
from .manufacturing.recorder import OpcUaRecorder
from .mcap_api import router as mcap_router
from .mqtt_api import router as mqtt_router
from .mqtt_feed import MqttFeed
from .opc_ua import OpcUaSession
from .opcua_api import connect_from_catalog, router as opcua_router
from .opcua_catalog import load_catalog
from .opcua_export import generate_opcua_artifacts, list_artifacts
from .repository import AasxRepository

logger = LogSetup.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def persistent_opcua_subscription(auto_connect: bool, mqtt_enabled: bool) -> bool:
    """시작 시 엣지를 상시 구독할지. MQTT 경로에서는 발행 프로세스가 그 구독을 맡는다."""
    return auto_connect and not mqtt_enabled


def _record_endpoint(opcua: OpcUaSession) -> str | None:
    """DB 기록용 엣지 주소. MQTT 경로면 카탈로그, OPC-UA 경로면 현재 세션."""
    if settings.mqtt.enabled:
        return _field_record_endpoint()
    return opcua.status().get("endpoint")


def _sync_context(opcua: OpcUaSession) -> SyncContext | None:
    """메타를 올릴 엣지. 기록기와 같은 엔드포인트와 카탈로그 AAS 를 쓴다."""
    try:
        server = load_catalog().default_server()
    except Exception:
        logger.warning("패키지 동기화용 OPC-UA 카탈로그를 읽지 못했습니다.", exc_info=True)
        return None
    if server is None:
        return None
    endpoint = _record_endpoint(opcua) or server.endpoint
    if not endpoint:
        return None
    return SyncContext(endpoint=endpoint, namespace=server.node_id_namespace, aas_ids=frozenset(server.aas_ids))


def _field_record_endpoint() -> str | None:
    """MQTT 수신을 DB에 넣을 때 설비 인덱스는 카탈로그의 엣지 주소를 본다."""
    try:
        server = load_catalog().default_server()
    except Exception:
        logger.warning("기록용 OPC-UA 엔드포인트를 읽지 못했습니다.", exc_info=True)
        return None
    return server.endpoint if server is not None else None


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
        ",".join(server.aas_ids) or "-",
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
        ",".join(server.aas_ids) or "-",
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
    repository = AasxRepository(app_settings.storage)
    opcua = OpcUaSession(app_settings.opcua)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage = app_settings.storage
        logger.info("DATA_ROOT: %s", storage.root)
        logger.info("AASX packages: %s", storage.packages_dir)
        logger.info("Archive: %s", storage.archive_dir)
        logger.info("OPC-UA catalog: %s", storage.opcua_catalog_dir)
        logger.info("Mappings: %s", storage.mappings_dir)
        if app_settings.mcap.enabled:
            archive = app_settings.mcap.archive_dir
            logger.info(
                "MCAP 탭: archive=%s (%s)",
                archive,
                "있음" if archive and archive.is_dir() else "없음",
            )
        repository.reload()
        _ensure_artifacts(repository)
        mfg = app_settings.manufacturing
        runtime = None
        feed = None
        if app_settings.mqtt.enabled:
            feed = MqttFeed(app_settings.mqtt, opcua)
            app.state.mqtt_feed = feed
        if mfg.enabled:
            # 엔진은 연결을 미리 열지 않는다. DB 가 꺼져 있어도 앱은 뜨고, /api/v1/timeseries 만 503 을 돌려준다.
            engine = create_engine(mfg)
            runtime = ManufacturingRuntime(mfg, engine, create_sessionmaker(engine), NodeCache(), opcua=opcua)
            if mfg.record_opcua:
                # 수신 콜백에 리스너만 붙인다 (REAL 설비로 기록). 경로는 전환 후에도 같은 함수가 본다.
                runtime.recorder = OpcUaRecorder(runtime, lambda: _record_endpoint(opcua))
                opcua.add_listener(runtime.recorder.on_change)
                runtime.recorder.start()
            if mfg.alarms_enabled:
                runtime.alarms = AlarmEngine(runtime)
                runtime.alarms.start()
            if mfg.sync_packages:
                runtime.package_sync = PackageSync(
                    runtime,
                    app_settings.storage.packages_dir,
                    app_settings.storage.package_opcua_dir,
                    lambda: _sync_context(opcua),
                )
                runtime.package_sync.start()
                logger.info(
                    "AASX 패키지 동기화: dir=%s interval=%ss",
                    app_settings.storage.packages_dir,
                    runtime.package_sync.interval,
                )
            app.state.manufacturing = runtime
            logger.info(
                "TimescaleDB: %s:%s/%s (record=%s source=%s)",
                mfg.db_host,
                mfg.db_port,
                mfg.db_name,
                mfg.record_opcua,
                "mqtt" if feed is not None else "opcua",
            )
        try:
            if feed is not None:
                await feed.start()
                logger.info(
                    "실시간 값은 MQTT %s:%s/%s 에서 받습니다. 시작 시 OPC-UA 상시 구독은 하지 않습니다.",
                    app_settings.mqtt.host,
                    app_settings.mqtt.port,
                    app_settings.mqtt.topic_prefix,
                )
            elif persistent_opcua_subscription(mfg.auto_connect_opcua, app_settings.mqtt.enabled):
                await _startup_opcua(opcua, repository)
            yield
        finally:
            try:
                if runtime is not None and runtime.package_sync is not None:
                    await runtime.package_sync.stop()
                if runtime is not None and runtime.alarms is not None:
                    await runtime.alarms.stop()
                if feed is not None:
                    await feed.stop()
                await opcua.disconnect()
                if runtime is not None and runtime.recorder is not None:
                    await runtime.recorder.stop()
            finally:
                if runtime is not None:
                    await runtime.engine.dispose()

    app = FastAPI(
        title="CAMTIC AAS Data Platform API",
        description="Eclipse BaSyx Python SDK로 AASX를 파싱·관리하고 OPC-UA 산출물 생성·현장 서버 조회를 제공하는 API",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.opcua = opcua
    app.state.manufacturing = None
    app.state.mqtt_feed = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Captured-Values"],
    )
    app.include_router(router)
    app.include_router(opcua_router)
    app.include_router(mqtt_router)
    app.include_router(camera_router)
    app.include_router(manufacturing_router)
    app.include_router(mcap_router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="viewer")
    return app


app = create_app()
