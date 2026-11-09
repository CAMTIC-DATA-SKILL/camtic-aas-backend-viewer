"""현장 OPC-UA 노드 점검 (읽기 전용). 서버 설정을 바꾸지 않는다 — 속성 읽기, 주소 공간 탐색, 짧은 구독만 한다.

    python -m scripts.opcua_field_audit                  # ROBOT_FIELD_01 의 23개 노드, 구독 2종 × 20초
    python -m scripts.opcua_field_audit --seconds 30 --out docs/field-opcua-audit.json

확인 항목
- 노드별: 존재 여부(NodeClass/BrowseName), 서버 DataType, MinimumSamplingInterval, AccessLevel, 현재 값·StatusCode·시각
- 없는 노드: 같은 BrowseName 을 주소 공간에서 찾아 NodeId 불일치인지, 서버에 아예 없는지 구분
- 구독 A (앱과 같음: publishing 500ms, sampling 100ms, queue 0) / 구독 B (publishing 100ms, sampling 50ms, queue 10):
  서버가 승인한 값(Revised Publishing / Sampling / Queue)과 SourceTimestamp · ServerTimestamp · 수신 시각 ·
  값 변경 간격을 따로 잰다. B 에서도 SourceTimestamp 간격이 같으면 갱신 주기는 클라이언트 설정이 아니라
  서버 앞단(서버의 데이터 갱신 또는 그 이전 단계)에서 정해진 것이다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asyncua import Client, ua

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.manufacturing.aas_link import package_nodes  # noqa: E402
from app.opcua_catalog import load_catalog  # noqa: E402
from app.repository import AasxRepository  # noqa: E402
from config import settings  # noqa: E402

PACKAGE = "HH7_AAS_Template_v0.2"


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(ordered), "median_ms": round(statistics.median(ordered), 1),
            "p05_ms": round(ordered[int(0.05 * (len(ordered) - 1))], 1),
            "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))], 1),
            "min_ms": round(ordered[0], 1), "max_ms": round(ordered[-1], 1)}


async def read_attributes(client: Client, node_id: str) -> dict[str, Any]:
    node = client.get_node(node_id)
    attrs = [ua.AttributeIds.NodeClass, ua.AttributeIds.BrowseName, ua.AttributeIds.DataType,
             ua.AttributeIds.MinimumSamplingInterval, ua.AttributeIds.AccessLevel, ua.AttributeIds.Value]
    values = await node.read_attributes(attrs)
    status = values[0].StatusCode
    if not status.is_good():
        return {"exists": False, "status": status.name}
    data_type = values[2].Value.Value
    try:
        type_name = (await client.get_node(data_type).read_browse_name()).Name if data_type else None
    except Exception:
        type_name = str(data_type)
    dv = values[5]
    return {
        "exists": True,
        "browse_name": values[1].Value.Value.Name,
        "server_data_type": type_name,
        "minimum_sampling_interval_ms": values[3].Value.Value,
        "access_level": values[4].Value.Value,
        "value": None if dv.Value is None else str(dv.Value.Value)[:60],
        "value_status": dv.StatusCode.name,
        "source_ts": _utc(dv.SourceTimestamp).isoformat() if dv.SourceTimestamp else None,
        "server_ts": _utc(dv.ServerTimestamp).isoformat() if dv.ServerTimestamp else None,
    }


async def find_by_browse_name(client: Client, names: set[str], limit: int = 5000) -> dict[str, list[str]]:
    """Objects 아래를 너비 우선으로 탐색해 BrowseName 이 같은 Variable 의 NodeId 를 모은다."""
    found: dict[str, list[str]] = defaultdict(list)
    queue = [client.nodes.objects]
    seen = 0
    while queue and seen < limit:
        node = queue.pop(0)
        try:
            children = await node.get_children()
        except Exception:
            continue
        for child in children:
            seen += 1
            try:
                name = (await child.read_browse_name()).Name
            except Exception:
                continue
            if name in names:
                found[name].append(child.nodeid.to_string())
            if child.nodeid.NamespaceIndex != 0:
                queue.append(child)
    return dict(found)


class Recorder:
    def __init__(self) -> None:
        self.events: dict[str, list[tuple[float, datetime | None, datetime | None, Any]]] = defaultdict(list)

    def datachange_notification(self, node, val, data):
        dv = data.monitored_item.Value
        self.events[node.nodeid.to_string()].append(
            (time.time(), _utc(dv.SourceTimestamp), _utc(dv.ServerTimestamp), val))


async def subscription_test(client: Client, node_ids: list[str], publishing: int, sampling: int, queue: int,
                            seconds: float) -> dict[str, Any]:
    handler = Recorder()
    sub = await client.create_subscription(publishing, handler)
    captured: list[Any] = []
    original = sub.server.create_monitored_items

    async def capture(params):  # 서버 응답(Revised 값)을 기록만 한다
        results = await original(params)
        captured.extend(results)
        return results

    sub.server.create_monitored_items = capture
    try:
        await sub.subscribe_data_change([client.get_node(n) for n in node_ids], sampling_interval=sampling,
                                        queuesize=queue)
    finally:
        sub.server.create_monitored_items = original
    revised_publishing = float(sub.parameters.RequestedPublishingInterval)
    await asyncio.sleep(seconds)
    await sub.delete()

    per_node: dict[str, Any] = {}
    for node_id, result in zip(node_ids, captured):
        events = handler.events.get(node_id, [])
        src = [e[1] for e in events if e[1]]
        srv = [e[2] for e in events if e[2]]
        rcv = [e[0] for e in events]
        changes = []
        last_value, last_change = object(), None
        for e in events:
            if e[3] != last_value:
                if last_change is not None and e[1]:
                    changes.append((e[1] - last_change).total_seconds() * 1000)
                last_value, last_change = e[3], e[1]
        per_node[node_id] = {
            "status": result.StatusCode.name,
            "revised_sampling_ms": result.RevisedSamplingInterval,
            "revised_queue_size": result.RevisedQueueSize,
            "notifications": len(events),
            "rate_hz": round(len(events) / seconds, 2),
            "source_ts_interval": _stats([(b - a).total_seconds() * 1000 for a, b in zip(src, src[1:]) if b > a]),
            "server_ts_interval": _stats([(b - a).total_seconds() * 1000 for a, b in zip(srv, srv[1:]) if b > a]),
            "client_receive_interval": _stats([(b - a) * 1000 for a, b in zip(rcv, rcv[1:])]),
            "value_change_interval": _stats(changes),
            "server_minus_source_ms": _stats([(e[2] - e[1]).total_seconds() * 1000 for e in events if e[1] and e[2]]),
            "receive_minus_source_ms": _stats([(e[0] - e[1].timestamp()) * 1000 for e in events if e[1]]),
        }
    return {"requested": {"publishing_ms": publishing, "sampling_ms": sampling, "queue_size": queue},
            "revised_publishing_ms": revised_publishing, "seconds": seconds, "nodes": per_node}


async def main(seconds: float) -> dict[str, Any]:
    repository = AasxRepository(settings.storage)
    repository.reload()
    server = load_catalog().default_server()
    nodes = package_nodes(repository.get(PACKAGE), repository, server.node_id_namespace)
    client = Client(server.endpoint, timeout=5)
    client.session_timeout = 600_000
    await client.connect()
    report: dict[str, Any] = {"endpoint": server.endpoint, "checked_at": datetime.now(timezone.utc).isoformat(),
                              "package": PACKAGE, "nodes": []}
    try:
        report["namespaces"] = await client.get_namespace_array()
        for n in nodes:
            info = await read_attributes(client, n.node_id)
            report["nodes"].append({"aas_path": f"{n.submodel_id_short}/{n.path}", "node_id": n.node_id,
                                    "aas_data_type": n.data_type, "unit": n.unit, **info})
        missing = {entry["aas_path"].split("/")[-1].split(".")[-1] for entry in report["nodes"] if not entry["exists"]}
        report["browse_search"] = await find_by_browse_name(client, missing) if missing else {}
        existing = [entry["node_id"] for entry in report["nodes"] if entry["exists"]]
        report["subscription_app_like"] = await subscription_test(client, existing, 500, 100, 0, seconds)
        report["subscription_fast"] = await subscription_test(client, existing, 100, 50, 10, seconds)
    finally:
        await client.disconnect()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    result = asyncio.run(main(args.seconds))
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"저장: {args.out}")
    else:
        print(text)
