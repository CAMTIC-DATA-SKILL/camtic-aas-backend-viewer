from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import Settings, settings
from utils.log_setup import LogSetup

from .api import router
from .repository import AasxRepository

logger = LogSetup.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(app_settings: Settings = settings) -> FastAPI:
    repository = AasxRepository(app_settings.aasx_data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Loading AASX packages from %s", app_settings.aasx_data_dir)
        repository.reload()
        yield

    app = FastAPI(
        title="CAMTIC AAS Viewer API",
        description="Eclipse BaSyx Python SDK로 AASX 파일을 파싱하여 제공하는 API",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="viewer")
    return app


app = create_app()
