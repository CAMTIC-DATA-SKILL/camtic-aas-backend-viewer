import json
from pathlib import Path

from config import OpcUaConfig
from app.opcua_catalog import load_catalog


def _cfg(**updates) -> OpcUaConfig:
    values = dict(
        server_id="env-default",
        endpoint="opc.tcp://127.0.0.1:4840",
        client_name="test-client",
        application_uri="urn:test",
        timeout=4.0,
        watchdog_interval=1.0,
        session_timeout_ms=1000,
        auto_reconnect=True,
        reconnect_max_delay=30.0,
        connect_retry_interval=2.0,
        connect_max_retries=3,
        username=None,
        password=None,
        security_policy="None",
        security_mode="SignAndEncrypt",
        client_cert=None,
        client_key=None,
        server_cert=None,
        subscription_period_ms=500,
        sampling_interval_ms=100,
        node_ids=(),
    )
    values.update(updates)
    return OpcUaConfig(**values)


def test_load_catalog_and_local_override(tmp_path: Path):
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "defaultServerId": "lab",
                "servers": [
                    {
                        "id": "lab",
                        "name": "Lab",
                        "endpoint": "opc.tcp://192.168.1.10:4840",
                        "aasId": "urn:example:aas:lab",
                    },
                    {
                        "id": "field",
                        "name": "Field",
                        "endpoint": "opc.tcp://10.0.0.8:4840",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "catalog.local.json").write_text(
        json.dumps(
            {
                "defaultServerId": "field",
                "servers": [
                    {
                        "id": "field",
                        "name": "Field override",
                        "endpoint": "opc.tcp://10.0.0.9:4840",
                        "aasId": "urn:example:aas:hyundai:hh7:pickplace:01",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    catalog = load_catalog(tmp_path, fallback=_cfg())
    assert catalog.get("lab") is not None
    field = catalog.get("field")
    assert field is not None
    assert field.endpoint == "opc.tcp://10.0.0.9:4840"
    assert field.aas_id == "urn:example:aas:hyundai:hh7:pickplace:01"
    assert catalog.get("lab").aas_id == "urn:example:aas:lab"
    assert catalog.default_server_id == "field"
    assert "catalog.json" in catalog.source
    assert "catalog.local.json" in catalog.source
    assert "password" not in catalog.to_dict()["servers"][0]


def test_missing_catalog_falls_back_to_env(tmp_path: Path):
    catalog = load_catalog(tmp_path, fallback=_cfg(endpoint="opc.tcp://edge:4840"))
    assert catalog.source == "env"
    assert catalog.default_server_id == "env-default"
    assert catalog.servers[0].endpoint == "opc.tcp://edge:4840"


def test_invalid_catalog_requires_id_and_endpoint(tmp_path: Path):
    (tmp_path / "catalog.json").write_text(
        json.dumps({"servers": [{"name": "broken"}]}),
        encoding="utf-8",
    )
    try:
        load_catalog(tmp_path, fallback=_cfg())
    except ValueError as exc:
        assert "id/endpoint" in str(exc)
    else:
        raise AssertionError("expected ValueError")
