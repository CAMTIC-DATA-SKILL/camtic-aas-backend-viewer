"""애플리케이션 설정.

환경 변수(.env 포함)는 이 모듈에서만 읽고, 다른 모듈은 ``from config import settings`` 로 참조한다.
이미 설정된 OS 환경 변수가 .env 값보다 우선한다.
"""

import os
import re
from dataclasses import dataclass, field, replace
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
class ManufacturingConfig:
    """기존 운영 TimescaleDB 연결 설정. 개발 DB로 대체 연결하지 않는다."""

    enabled: bool
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    pool_size: int = 5
    pool_max_overflow: int = 5
    command_timeout: float = 10.0
    # 수집 시스템이 POST /api/v1/ingest 할 때 쓰는 키. 비어 있으면 ingest 를 받지 않는다.
    ingest_api_key: Optional[str] = field(default=None, repr=False)
    # 설정하면 조회 API 도 X-API-Key 를 요구한다. 비우면 조회는 키 없이 허용 (로컬 바인딩 전제).
    read_api_key: Optional[str] = field(default=None, repr=False)
    # 기존 OPC-UA 구독 수신값을 REAL 설비로 기록한다 (구독은 그대로, 리스너만 붙음).
    record_opcua: bool = False
    # 시작 시 OPC-UA 자동 접속. 원본 동작(true)이 기본이다. 승인 전 운영 서버에서 실제 접속을 막을 때만 .env 에서 false.
    # MQTT_ENABLED=true 이면 이 값과 관계없이 시작 시 상시 구독은 하지 않는다.
    auto_connect_opcua: bool = True
    equipment_id: str = "ROBOT_FIELD_01"
    # 수신 중단·복구, Bad/Uncertain 품질, 임계값 초과를 alarm_event 에 기록한다.
    alarms_enabled: bool = False
    # packages/ 의 AASX 템플릿 engineering.csv 를 주기적으로 읽어 설비·노드·AAS 매핑을 올린다.
    sync_packages: bool = True
    sync_interval_s: float = 60.0

def load_manufacturing_config() -> ManufacturingConfig:
    enabled = _bool("MFG_ENABLED", False)
    cfg = ManufacturingConfig(
        enabled=enabled,
        db_host=_str("MFG_DB_HOST", "127.0.0.1"),
        db_port=_int("MFG_DB_PORT", 25432),
        db_name=_str("MFG_DB_NAME", "seongjang_testbed"),
        db_user=_str("MFG_DB_USER", ""),
        db_password=_str("MFG_DB_PASSWORD", "") if enabled else os.environ.get("MFG_DB_PASSWORD", ""),
        pool_size=max(1, _int("MFG_DB_POOL_SIZE", 5)),
        pool_max_overflow=max(0, _int("MFG_DB_POOL_MAX_OVERFLOW", 5)),
        command_timeout=_float("MFG_DB_COMMAND_TIMEOUT", 10.0),
        ingest_api_key=_optional_str("MFG_INGEST_API_KEY"),
        read_api_key=_optional_str("MFG_READ_API_KEY"),
        record_opcua=_bool("MFG_RECORD_OPCUA", False),
        auto_connect_opcua=_bool("OPCUA_AUTO_CONNECT", True),
        equipment_id=_str("MFG_EQUIPMENT_ID", "ROBOT_FIELD_01"),
        alarms_enabled=_bool("MFG_ALARMS_ENABLED", False),
        sync_packages=_bool("MFG_SYNC_PACKAGES", True),
        sync_interval_s=max(5.0, _float("MFG_SYNC_INTERVAL_S", 60.0)),
    )
    if enabled and not cfg.db_password:
        raise RuntimeError("MFG_ENABLED=true 이면 MFG_DB_PASSWORD 가 필요합니다. .env 를 확인하세요.")
    if enabled and not cfg.db_user:
        raise RuntimeError("MFG_ENABLED=true 이면 운영 계정 MFG_DB_USER가 필요합니다.")
    # 이전 개발용 .env가 배포에 섞이면 연결 전에 중단한다. 운영 DB로 자동 대체하지 않는다.
    if cfg.db_port == 55432 or cfg.db_name == "manufacturing_db":
        raise RuntimeError(
            "개발 DB 설정은 더 이상 사용하지 않습니다. .env의 MFG_DB_PORT=25432, "
            "MFG_DB_NAME=seongjang_testbed 및 운영 DB 계정을 설정하세요."
        )
    return cfg


def _under_root(root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def _single_segment(label: str, raw: str) -> str:
    name = raw.strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).name != name
    ):
        raise RuntimeError(f"{label} 은(는) 폴더·파일 이름 하나여야 합니다: {raw!r}")
    return name


def _package_id(package_id: str) -> str:
    if (
        not package_id
        or package_id in {".", ".."}
        or "/" in package_id
        or "\\" in package_id
        or "\x00" in package_id
    ):
        raise ValueError(f"잘못된 패키지 id: {package_id}")
    return package_id


@dataclass(frozen=True)
class StorageLayout:
    """DATA_ROOT 아래 저장 트리.

    packages/{package_id}/package.aasx 가 기준 원본이고,
    같은 폴더의 opcua·cad·usd·config 에 산출물과 작업 사본을 둔다.
    디렉터리 이름은 .env 에서 오고, 비어 있으면 data/ 기준 fallback 을 쓴다.
    """

    root: Path
    packages_dir: Path
    archive_dir: Path
    site_config_dir: Path
    opcua_catalog_dir: Path
    mappings_dir: Path
    package_aasx_name: str
    package_opcua_dir: str
    package_cad_dir: str
    package_usd_dir: str
    package_config_dir: str

    def ensure_tree(self) -> None:
        for path in (
            self.packages_dir,
            self.archive_dir,
            self.site_config_dir,
            self.opcua_catalog_dir,
            self.mappings_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def package_dir(self, package_id: str) -> Path:
        return self.packages_dir / _package_id(package_id)

    def aasx_path(self, package_id: str) -> Path:
        return self.package_dir(package_id) / self.package_aasx_name

    def opcua_dir(self, package_id: str) -> Path:
        return self.package_dir(package_id) / self.package_opcua_dir

    def cad_dir(self, package_id: str) -> Path:
        return self.package_dir(package_id) / self.package_cad_dir

    def usd_dir(self, package_id: str) -> Path:
        return self.package_dir(package_id) / self.package_usd_dir

    def package_config_path(self, package_id: str) -> Path:
        return self.package_dir(package_id) / self.package_config_dir


def resolve_storage(
    root: Path,
    *,
    packages_dir: str = "packages",
    archive_dir: str = "archive",
    site_config_dir: str = "config",
    opcua_catalog_dir: str = "config/opcua",
    mappings_dir: str = "config/mappings",
    package_aasx_name: str = "package.aasx",
    package_opcua_dir: str = "opcua",
    package_cad_dir: str = "cad",
    package_usd_dir: str = "usd",
    package_config_dir: str = "config",
) -> StorageLayout:
    """상대 경로는 root 아래에 붙인다. 기본 이름은 DATA_ROOT=data 일 때의 fallback 이다."""
    aasx_name = _single_segment("PACKAGE_AASX_NAME", package_aasx_name)
    if not aasx_name.lower().endswith(".aasx"):
        raise RuntimeError(f"PACKAGE_AASX_NAME 은 .aasx 로 끝나야 합니다: {aasx_name!r}")
    resolved = root.resolve()
    return StorageLayout(
        root=resolved,
        packages_dir=_under_root(resolved, packages_dir),
        archive_dir=_under_root(resolved, archive_dir),
        site_config_dir=_under_root(resolved, site_config_dir),
        opcua_catalog_dir=_under_root(resolved, opcua_catalog_dir),
        mappings_dir=_under_root(resolved, mappings_dir),
        package_aasx_name=aasx_name,
        package_opcua_dir=_single_segment("PACKAGE_OPCUA_DIR", package_opcua_dir),
        package_cad_dir=_single_segment("PACKAGE_CAD_DIR", package_cad_dir),
        package_usd_dir=_single_segment("PACKAGE_USD_DIR", package_usd_dir),
        package_config_dir=_single_segment("PACKAGE_CONFIG_DIR", package_config_dir),
    )


def load_storage() -> StorageLayout:
    """DATA_ROOT 와 하위 경로는 환경 변수가 있으면 그 값을 쓰고, 없으면 data/ fallback 을 쓴다."""
    root = _path(os.environ.get("DATA_ROOT", "").strip() or "data")
    overrides: dict[str, str] = {}
    for env_name, field in (
        ("PACKAGES_DIR", "packages_dir"),
        ("ARCHIVE_DIR", "archive_dir"),
        ("SITE_CONFIG_DIR", "site_config_dir"),
        ("OPCUA_CATALOG_DIR", "opcua_catalog_dir"),
        ("MAPPINGS_DIR", "mappings_dir"),
        ("PACKAGE_AASX_NAME", "package_aasx_name"),
        ("PACKAGE_OPCUA_DIR", "package_opcua_dir"),
        ("PACKAGE_CAD_DIR", "package_cad_dir"),
        ("PACKAGE_USD_DIR", "package_usd_dir"),
        ("PACKAGE_CONFIG_DIR", "package_config_dir"),
    ):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            overrides[field] = raw
    return resolve_storage(root, **overrides)


@dataclass(frozen=True)
class MqttConfig:
    """실시간 값 브로커.

    ``enabled`` 이면 AAS 서버가 구독하고, 시작 시 OPC-UA 상시 구독은 하지 않는다.
    발행 프로세스(``python -m publisher``)는 같은 주소로 값을 올린다.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    topic_prefix: str = "aas"
    client_id: str = "camtic-aas-subscriber"
    publisher_client_id: str = "camtic-aas-publisher"
    keepalive: int = 60


def write_env_value(path: Path, key: str, value: str) -> None:
    """``.env`` 의 한 줄만 바꾼다. 다른 줄과 주석은 그대로 둔다."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    line = f"{key}={value}"
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    path.write_text(text, encoding="utf-8")


def set_mqtt_enabled(enabled: bool, env_path: Path | None = None) -> MqttConfig:
    """MQTT 실시간 경로를 ``.env`` 와 이 프로세스 설정에 같이 반영한다."""
    flag = "true" if enabled else "false"
    write_env_value(env_path or (BASE_DIR / ".env"), "MQTT_ENABLED", flag)
    os.environ["MQTT_ENABLED"] = flag
    updated = replace(settings.mqtt, enabled=enabled)
    object.__setattr__(settings, "mqtt", updated)
    return updated


def load_mqtt_config() -> MqttConfig:
    prefix = _str("MQTT_TOPIC_PREFIX", "aas").strip().strip("/")
    if not prefix or any(part in {"+", "#"} or "+" in part or "#" in part for part in prefix.split("/")):
        raise RuntimeError(f"MQTT_TOPIC_PREFIX 는 빈 값이나 와일드카드(+, #)일 수 없습니다: {prefix!r}")
    port = _int("MQTT_PORT", 1883)
    if not 1 <= port <= 65535:
        raise RuntimeError(f"MQTT_PORT 는 1~65535 여야 합니다: {port}")
    keepalive = _int("MQTT_KEEPALIVE", 60)
    if keepalive < 1:
        raise RuntimeError(f"MQTT_KEEPALIVE 는 1 이상이어야 합니다: {keepalive}")
    client_id = _str("MQTT_CLIENT_ID", "camtic-aas-subscriber")
    publisher_client_id = _str("MQTT_PUBLISHER_CLIENT_ID", "camtic-aas-publisher")
    if not client_id or not publisher_client_id:
        raise RuntimeError("MQTT_CLIENT_ID 와 MQTT_PUBLISHER_CLIENT_ID 는 비울 수 없습니다.")
    return MqttConfig(
        enabled=_bool("MQTT_ENABLED", False),
        host=_str("MQTT_HOST", "127.0.0.1"),
        port=port,
        username=_optional_str("MQTT_USERNAME"),
        password=_optional_str("MQTT_PASSWORD"),
        topic_prefix=prefix,
        client_id=client_id,
        publisher_client_id=publisher_client_id,
        keepalive=keepalive,
    )


@dataclass(frozen=True)
class Settings:
    storage: StorageLayout

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
    manufacturing: ManufacturingConfig
    mqtt: MqttConfig


def load_settings() -> Settings:
    log_dir = os.environ.get("LOG_DIR", "").strip()
    storage = load_storage()
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
        storage=storage,
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
        manufacturing=load_manufacturing_config(),
        mqtt=load_mqtt_config(),
    )


settings = load_settings()
