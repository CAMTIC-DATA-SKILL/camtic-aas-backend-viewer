"""SQLAlchemy 2.x async 엔진 (asyncpg)."""

from __future__ import annotations

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from config import ManufacturingConfig

SCHEMA = "manufacturing"


def database_url(cfg: ManufacturingConfig) -> URL:
    return URL.create(
        "postgresql+asyncpg",
        username=cfg.db_user,
        password=cfg.db_password,
        host=cfg.db_host,
        port=cfg.db_port,
        database=cfg.db_name,
    )


def create_engine(cfg: ManufacturingConfig) -> AsyncEngine:
    return create_async_engine(
        database_url(cfg),
        pool_size=cfg.pool_size,
        max_overflow=cfg.pool_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "command_timeout": cfg.command_timeout,
            "server_settings": {"application_name": "camtic_manufacturing", "timezone": "UTC"},
        },
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
