import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import Settings, load_settings
from .repository import AasxRepository

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    repository = AasxRepository(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.getLogger(__name__).info("Loading AASX packages from %s", settings.data_dir)
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
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="viewer")
    return app


app = create_app()
