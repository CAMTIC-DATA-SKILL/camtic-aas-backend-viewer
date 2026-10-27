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


def _optional_str(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def _bool(name: str, default: bool) -> bool:
    raw = _str(name, str(default)).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"환경 변수 {name} 은(는) true/false 여야 합니다: {raw!r}")


def _float(name: str, default: float) -> float:
    raw = _str(name, str(default))
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"환경 변수 {name} 은(는) 실수여야 합니다: {raw!r}") from None


def _optional_path(name: str) -> Optional[Path]:
    raw = os.environ.get(name, "").strip()
    return _path(raw) if raw else None


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
class OpcUaConfig:
    """현장 OPC-UA 서버에 붙는 클라이언트 설정. middleware_gateway_opc_ua 와 같은 키를 쓴다."""

    server_id: str
    endpoint: str
    client_name: str
    application_uri: str

    timeout: float
    watchdog_interval: float
    session_timeout_ms: int

    auto_reconnect: bool
    reconnect_max_delay: float
    connect_retry_interval: float
    connect_max_retries: int

    username: Optional[str]
    password: Optional[str]

    security_policy: str
    security_mode: str
    client_cert: Optional[Path]
    client_key: Optional[Path]
    server_cert: Optional[Path]

    subscription_period_ms: int
    sampling_interval_ms: int
    node_ids: tuple[str, ...]

    @property
    def use_security(self) -> bool:
        return self.security_policy.lower() != "none"


@dataclass(frozen=True)
class Settings:
    aasx_data_dir: Path
    artifacts_dir: Path

    server_host: str
    server_port: int
    server_reload: bool
    cors_origins: tuple[str, ...]

    log_level: str
    log_dir: Optional[Path]
    log_file_name: str
    log_max_bytes: int
    log_backup_count: int

    opcua: OpcUaConfig

    collector_url: Optional[str]
    record_dir: Path
    record_interval: float


def load_settings() -> Settings:
    log_dir = os.environ.get("LOG_DIR", "").strip()
    aasx_data_dir = _path(_str("AASX_DATA_DIR"))
    artifacts_raw = os.environ.get("ARTIFACTS_DIR", "").strip()
    artifacts_dir = _path(artifacts_raw) if artifacts_raw else (aasx_data_dir.parent / "artifacts")
    opcua = OpcUaConfig(
        server_id=_str("OPCUA_SERVER_ID", "default"),
        endpoint=_str("OPCUA_ENDPOINT", "opc.tcp://127.0.0.1:4840"),
        client_name=_str("OPCUA_CLIENT_NAME", "camtic_aas_backend_viewer"),
        application_uri=_str("OPCUA_APPLICATION_URI", "urn:camtic:aas:opcua:client"),
        timeout=_float("OPCUA_TIMEOUT", 4.0),
        watchdog_interval=_float("OPCUA_WATCHDOG_INTERVAL", 1.0),
        session_timeout_ms=_int("OPCUA_SESSION_TIMEOUT_MS", 3_600_000),
        auto_reconnect=_bool("OPCUA_AUTO_RECONNECT", True),
        reconnect_max_delay=_float("OPCUA_RECONNECT_MAX_DELAY", 30.0),
        connect_retry_interval=_float("OPCUA_CONNECT_RETRY_INTERVAL", 2.0),
        connect_max_retries=_int("OPCUA_CONNECT_MAX_RETRIES", 3),
        username=_optional_str("OPCUA_USERNAME"),
        password=_optional_str("OPCUA_PASSWORD"),
        security_policy=_str("OPCUA_SECURITY_POLICY", "None"),
        security_mode=_str("OPCUA_SECURITY_MODE", "SignAndEncrypt"),
        client_cert=_optional_path("OPCUA_CLIENT_CERT"),
        client_key=_optional_path("OPCUA_CLIENT_KEY"),
        server_cert=_optional_path("OPCUA_SERVER_CERT"),
        subscription_period_ms=_int("OPCUA_SUBSCRIPTION_PERIOD_MS", 500),
        sampling_interval_ms=_int("OPCUA_SAMPLING_INTERVAL_MS", 100),
        node_ids=_list("OPCUA_NODE_IDS", ""),
    )
    if opcua.use_security and not (opcua.client_cert and opcua.client_key):
        raise RuntimeError(
            "OPCUA_SECURITY_POLICY 가 None 이 아니면 OPCUA_CLIENT_CERT, OPCUA_CLIENT_KEY 가 필요합니다."
        )

    return Settings(
        aasx_data_dir=aasx_data_dir,
        artifacts_dir=artifacts_dir,
        server_host=_str("SERVER_HOST", "127.0.0.1"),
        server_port=_int("SERVER_PORT", 8000),
        server_reload=_bool("SERVER_RELOAD", False),
        cors_origins=_list("CORS_ORIGINS", "*"),
        log_level=_log_level("LOG_LEVEL", "INFO"),
        log_dir=_path(log_dir) if log_dir else None,
        log_file_name=_str("LOG_FILE_NAME", "app.log"),
        log_max_bytes=_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
        log_backup_count=_int("LOG_BACKUP_COUNT", 5),
        opcua=opcua,
        collector_url=_optional_str("COLLECTOR_URL"),
        record_dir=_path(_str("RECORD_DIR", "data/records")),
        record_interval=_float("RECORD_INTERVAL", 1.0),
    )


settings = load_settings()
