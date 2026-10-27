"""OPC-UA 서버 카탈로그.

접속 정보(엔드포인트·보안 정책)만 둔다. 구독 토픽은 mapping.json 이다.

찾는 순서:
1. `{AASX_DATA_DIR}/../opcua/catalog.json`
2. 같은 폴더의 `catalog.local.json` 이 있으면 id 기준으로 덮어씀
3. 파일이 없으면 `.env` 의 OPCUA_* 한 대를 카탈로그로 쓴다

비밀번호는 카탈로그에 넣지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from config import OpcUaConfig, settings

CATALOG_FILENAME = "catalog.json"
LOCAL_CATALOG_FILENAME = "catalog.local.json"


@dataclass(frozen=True)
class CatalogServer:
    id: str
    name: str
    endpoint: str
    security_policy: str = "None"
    security_mode: str = "SignAndEncrypt"
    aas_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "securityPolicy": self.security_policy,
            "securityMode": self.security_mode,
        }
        if self.aas_id:
            payload["aasId"] = self.aas_id
        return payload


@dataclass(frozen=True)
class OpcUaCatalog:
    source: str
    default_server_id: str
    servers: tuple[CatalogServer, ...] = ()

    def get(self, server_id: str) -> Optional[CatalogServer]:
        key = (server_id or "").strip()
        if not key:
            return None
        for server in self.servers:
            if server.id == key:
                return server
        return None

    def default_server(self) -> Optional[CatalogServer]:
        return self.get(self.default_server_id) or (self.servers[0] if self.servers else None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "defaultServerId": self.default_server_id or None,
            "servers": [server.to_dict() for server in self.servers],
        }


def catalog_dir() -> Path:
    return settings.aasx_data_dir.parent / "opcua"


def catalog_candidates(directory: Path | None = None) -> list[Path]:
    root = directory or catalog_dir()
    return [root / CATALOG_FILENAME, root / LOCAL_CATALOG_FILENAME]


def load_catalog(
    directory: Path | None = None,
    fallback: OpcUaConfig | None = None,
) -> OpcUaCatalog:
    root = directory or catalog_dir()
    base_path, local_path = catalog_candidates(root)
    catalog: OpcUaCatalog | None = None
    sources: list[str] = []

    if base_path.is_file():
        catalog = _read_catalog(base_path)
        sources.append(str(base_path))
    if local_path.is_file():
        local = _read_catalog(local_path)
        sources.append(str(local_path))
        catalog = _merge_catalog(catalog, local) if catalog else local

    if catalog is None:
        return _from_config(fallback or settings.opcua)

    return OpcUaCatalog(
        source="+".join(sources),
        default_server_id=catalog.default_server_id,
        servers=catalog.servers,
    )


def _from_config(cfg: OpcUaConfig) -> OpcUaCatalog:
    server = CatalogServer(
        id=cfg.server_id or "default",
        name=cfg.client_name or cfg.server_id or "default",
        endpoint=cfg.endpoint,
        security_policy=cfg.security_policy,
        security_mode=cfg.security_mode,
    )
    return OpcUaCatalog(source="env", default_server_id=server.id, servers=(server,))


def _read_catalog(path: Path) -> OpcUaCatalog:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"카탈로그는 객체여야 합니다: {path}")
    servers = tuple(_parse_server(item, index) for index, item in enumerate(raw.get("servers") or []))
    if not servers:
        raise ValueError(f"카탈로그 서버가 없습니다: {path}")
    ids = [server.id for server in servers]
    if len(ids) != len(set(ids)):
        raise ValueError(f"카탈로그 서버 id 가 중복됩니다: {path}")
    default_id = str(raw.get("defaultServerId") or servers[0].id).strip()
    return OpcUaCatalog(source=str(path), default_server_id=default_id, servers=servers)


def _parse_server(raw: dict[str, Any], index: int) -> CatalogServer:
    if not isinstance(raw, dict):
        raise ValueError(f"servers[{index}] 는 객체여야 합니다.")
    server_id = str(raw.get("id") or "").strip()
    endpoint = str(raw.get("endpoint") or "").strip()
    if not server_id or not endpoint:
        raise ValueError(f"servers[{index}] 에 id/endpoint 가 필요합니다.")
    name = str(raw.get("name") or server_id).strip() or server_id
    return CatalogServer(
        id=server_id,
        name=name,
        endpoint=endpoint,
        security_policy=str(raw.get("securityPolicy") or "None").strip() or "None",
        security_mode=str(raw.get("securityMode") or "SignAndEncrypt").strip() or "SignAndEncrypt",
        aas_id=str(raw.get("aasId") or "").strip(),
    )


def _merge_catalog(base: OpcUaCatalog, overlay: OpcUaCatalog) -> OpcUaCatalog:
    by_id = {server.id: server for server in base.servers}
    for server in overlay.servers:
        by_id[server.id] = server
    default_id = overlay.default_server_id or base.default_server_id
    return OpcUaCatalog(
        source=f"{base.source}+{overlay.source}",
        default_server_id=default_id,
        servers=tuple(by_id.values()),
    )
