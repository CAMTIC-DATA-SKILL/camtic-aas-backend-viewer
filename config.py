"""애플리케이션 설정.

환경 변수(.env 포함)는 이 모듈에서만 읽고, 다른 모듈은 ``from config import settings`` 로 참조한다.
이미 설정된 OS 환경 변수가 .env 값보다 우선한다.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def _str(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if default is None:
        raise RuntimeError(f"환경 변수 {name} 이(가) 설정되지 않았습니다. .env 파일을 확인하세요.")
    return default


def _int(name: str, default: int) -> int:
    raw = _str(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"환경 변수 {name} 은(는) 정수여야 합니다: {raw!r}") from None


def _bool(name: str, default: bool) -> bool:
    raw = _str(name, str(default)).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"환경 변수 {name} 은(는) true/false 여야 합니다: {raw!r}")


def _path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in _str(name, default).split(",") if item.strip())


def _log_level(name: str, default: str) -> str:
    level = _str(name, default).upper()
    if level not in _LOG_LEVELS:
        raise RuntimeError(f"환경 변수 {name} 은(는) {', '.join(_LOG_LEVELS)} 중 하나여야 합니다: {level!r}")
    return level


@dataclass(frozen=True)
class Settings:
    aasx_data_dir: Path

    server_host: str
    server_port: int
    server_reload: bool
    cors_origins: tuple[str, ...]

    log_level: str
    log_dir: Optional[Path]
    log_file_name: str
    log_max_bytes: int
    log_backup_count: int


def load_settings() -> Settings:
    log_dir = os.environ.get("LOG_DIR", "").strip()
    return Settings(
        aasx_data_dir=_path(_str("AASX_DATA_DIR")),
        server_host=_str("SERVER_HOST", "127.0.0.1"),
        server_port=_int("SERVER_PORT", 8000),
        server_reload=_bool("SERVER_RELOAD", False),
        cors_origins=_list("CORS_ORIGINS", "*"),
        log_level=_log_level("LOG_LEVEL", "INFO"),
        log_dir=_path(log_dir) if log_dir else None,
        log_file_name=_str("LOG_FILE_NAME", "app.log"),
        log_max_bytes=_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
        log_backup_count=_int("LOG_BACKUP_COUNT", 5),
    )


settings = load_settings()
