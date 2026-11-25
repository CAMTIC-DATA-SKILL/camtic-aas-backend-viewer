"""manufacturing.opcua_sample 을 MCAP 으로 내보낸다.

노드 하나가 Foxglove 에서 그릴 수 있는 JSON 채널이 된다.
로그 시각은 opcua_sample.ts 다. 수집기는 장비 시각이 있으면 그 값을, 없으면 수신 시각을 ts 로 저장한다.

예 (camtic-aas-backend-viewer 루트):
    python scripts/export_mcap.py
    python scripts/export_mcap.py --equipment ROBOT_FIELD_01 --output ../mcap/hh7.mcap
    python scripts/export_mcap.py --equipment ROBOT_FIELD_01 \\
        --start 2026-11-11T09:00:00Z --end 2026-11-11T10:00:00Z \\
        --output ../mcap/HH7_20261111_090000.mcap
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT.parent / "mcap" / "opcua_sample.mcap"

NUMERIC_SCHEMA = {
    "title": "camtic.NumericSample",
    "type": "object",
    "properties": {
        "value": {"type": ["number", "null"]},
        "quality": {"type": "integer", "description": "OPC-UA StatusCode. 0 is Good."},
    },
}
BOOLEAN_SCHEMA = {
    "title": "camtic.BooleanSample",
    "type": "object",
    "properties": {
        "value": {"type": ["boolean", "null"]},
        "quality": {"type": "integer"},
    },
}
TEXT_SCHEMA = {
    "title": "camtic.TextSample",
    "type": "object",
    "properties": {
        "value": {"type": ["string", "null"]},
        "quality": {"type": "integer"},
    },
}
SCHEMAS = {
    "number": ("camtic.NumericSample", NUMERIC_SCHEMA),
    "boolean": ("camtic.BooleanSample", BOOLEAN_SCHEMA),
    "text": ("camtic.TextSample", TEXT_SCHEMA),
}
_BAD_NUMBERS = {b"", b"NaN", b"Infinity", b"-Infinity", b"nan", b"inf", b"-inf"}


@dataclass(frozen=True)
class Node:
    node_pk: int
    node_id: str
    node_name: str
    data_type: str
    unit: str
    equipment_id: str
    equipment_name: str
    kind: str
    topic: str


@dataclass
class Db:
    host: str
    port: str
    name: str
    user: str
    password: str

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PGPASSWORD"] = self.password
        env["PGCLIENTENCODING"] = "UTF8"
        env["LC_ALL"] = "C"
        env["PGOPTIONS"] = "-c statement_timeout=0 -c default_transaction_read_only=on"
        return env

    def psql(self, sql: str) -> str:
        proc = subprocess.run(
            [
                "psql", "-h", self.host, "-p", self.port, "-U", self.user, "-d", self.name,
                "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql,
            ],
            env=self.env(),
            check=False,
            capture_output=True,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            message = detail[-1] if detail else "psql failed"
            raise RuntimeError(message)
        return proc.stdout.decode("utf-8")


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def db_from_env() -> Db:
    load_env(ROOT / ".env")
    missing = [name for name in ("MFG_DB_HOST", "MFG_DB_PORT", "MFG_DB_NAME", "MFG_DB_USER", "MFG_DB_PASSWORD")
               if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(f"missing {' '.join(missing)}")
    return Db(
        host=os.environ["MFG_DB_HOST"].strip(),
        port=os.environ["MFG_DB_PORT"].strip(),
        name=os.environ["MFG_DB_NAME"].strip(),
        user=os.environ["MFG_DB_USER"].strip(),
        password=os.environ["MFG_DB_PASSWORD"].strip(),
    )


def kind_of(data_type: str) -> str:
    lowered = data_type.strip().lower()
    if lowered in {"boolean", "bool"}:
        return "boolean"
    if lowered in {"string", "text", "localizedtext", "qualifiedname", "datetime"}:
        return "text"
    return "number"


def topic_of(node_id: str) -> str:
    raw = node_id.split(";s=", 1)[1] if ";s=" in node_id else node_id
    return "/" + raw.strip("/")


def load_nodes(db: Db, equipment: str | None) -> list[Node]:
    where = ""
    if equipment:
        if not equipment.replace("_", "").replace("-", "").replace(".", "").isalnum():
            raise RuntimeError("equipment id has unsupported characters")
        where = f"WHERE n.equipment_id = '{equipment}'"
    raw = db.psql(f"""
        SELECT coalesce(json_agg(json_build_object(
            'node_pk', n.node_pk,
            'node_id', n.node_id,
            'node_name', n.node_name,
            'data_type', n.data_type,
            'unit', coalesce(n.unit, ''),
            'equipment_id', n.equipment_id,
            'equipment_name', e.equipment_name
        ) ORDER BY n.equipment_id, n.node_pk), '[]'::json)
        FROM manufacturing.equipment_latest l
        JOIN manufacturing.equipment_node n ON n.node_pk = l.node_pk
        JOIN manufacturing.equipment e ON e.equipment_id = n.equipment_id
        {where}
    """)
    rows = json.loads(raw.strip() or "[]")
    used: dict[str, int] = {}
    nodes: list[Node] = []
    for row in rows:
        topic = topic_of(row["node_id"])
        if topic in used:
            topic = f"{topic}/{row['node_pk']}"
        used[topic] = row["node_pk"]
        nodes.append(Node(
            node_pk=int(row["node_pk"]),
            node_id=row["node_id"],
            node_name=row["node_name"],
            data_type=row["data_type"],
            unit=row["unit"] or "",
            equipment_id=row["equipment_id"],
            equipment_name=row["equipment_name"],
            kind=kind_of(row["data_type"]),
            topic=topic,
        ))
    return nodes


def copy_sql(
    node: Node,
    limit_per_node: int | None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> str:
    if node.kind == "boolean":
        value_expr = "CASE WHEN value_bool THEN 'true' WHEN value_bool IS FALSE THEN 'false' ELSE '' END"
    elif node.kind == "text":
        value_expr = "value_text"
    else:
        value_expr = "value_num"
    where = [f"node_pk = {node.node_pk}"]
    if start is not None:
        where.append(f"ts >= '{start.astimezone(timezone.utc).isoformat()}'::timestamptz")
    if end is not None:
        where.append(f"ts < '{end.astimezone(timezone.utc).isoformat()}'::timestamptz")
    limit_sql = f" LIMIT {int(limit_per_node)}" if limit_per_node else ""
    return f"""COPY (
SELECT (floor(extract(epoch FROM ts) * 1000))::bigint, {value_expr}, quality
FROM manufacturing.opcua_sample
WHERE {' AND '.join(where)}
ORDER BY ts{limit_sql}
) TO STDOUT WITH (FORMAT csv, NULL '')"""


class SampleStream:
    def __init__(self, node: Node, proc: subprocess.Popen[bytes]) -> None:
        self.node = node
        self.proc = proc
        self.ts_ms = 0
        self.value = b""
        self.quality = b"0"

    def pull(self) -> bool:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            return False
        ts_b, value_b, quality_b = line.rstrip(b"\r\n").split(b",", 2)
        self.ts_ms = int(ts_b)
        self.value = value_b
        self.quality = quality_b if quality_b.isdigit() else b"0"
        return True


def numeric_payload(value: bytes, quality: bytes) -> bytes:
    if value in _BAD_NUMBERS:
        value = b"null"
    elif value in (b"-0", b"-0.0"):
        value = b"0"
    return b'{"value":' + value + b',"quality":' + quality + b"}"


def boolean_payload(value: bytes, quality: bytes) -> bytes:
    if value == b"true":
        body = b"true"
    elif value == b"false":
        body = b"false"
    else:
        body = b"null"
    return b'{"value":' + body + b',"quality":' + quality + b"}"


def text_payload(value: bytes, quality: bytes) -> bytes:
    text = None if value == b"" else value.decode("utf-8")
    return json.dumps({"value": text, "quality": int(quality)}, ensure_ascii=False, separators=(",", ":")).encode()


ENCODERS = {
    "number": numeric_payload,
    "boolean": boolean_payload,
    "text": text_payload,
}


def open_stream(
    db: Db,
    node: Node,
    limit_per_node: int | None,
    err_dir: Path,
    start: datetime | None = None,
    end: datetime | None = None,
) -> SampleStream:
    err_path = err_dir / f"{node.node_pk}.err"
    err_file = err_path.open("wb")
    proc = subprocess.Popen(
        [
            "psql", "-h", db.host, "-p", db.port, "-U", db.user, "-d", db.name,
            "-v", "ON_ERROR_STOP=1", "-q", "-c", copy_sql(node, limit_per_node, start, end),
        ],
        env=db.env(),
        stdout=subprocess.PIPE,
        stderr=err_file,
        bufsize=1024 * 1024,
    )
    err_file.close()
    return SampleStream(node, proc)


def stop_streams(streams: list[SampleStream]) -> None:
    for stream in streams:
        if stream.proc.poll() is None:
            stream.proc.kill()
    for stream in streams:
        stream.proc.wait()


def stream_error(stream: SampleStream, err_dir: Path) -> str | None:
    code = stream.proc.wait()
    if code == 0:
        return None
    err_path = err_dir / f"{stream.node.node_pk}.err"
    text = err_path.read_text("utf-8", "replace").strip().splitlines()
    detail = text[-1] if text else f"exit {code}"
    return f"node {stream.node.node_pk} {stream.node.topic}: {detail}"


def export(
    db: Db,
    nodes: list[Node],
    output: Path,
    limit_per_node: int | None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    err_dir = output.parent / f".{output.name}.errors"
    err_dir.mkdir(parents=True, exist_ok=True)
    streams: list[SampleStream] = []
    started = time.monotonic()
    written = 0
    try:
        streams = [open_stream(db, node, limit_per_node, err_dir, start, end) for node in nodes]
        with partial.open("wb") as handle:
            writer = Writer(handle, chunk_size=4 * 1024 * 1024, compression=CompressionType.ZSTD)
            writer.start(profile="", library="camtic-aas-backend-viewer export_mcap")
            schema_ids = {
                kind: writer.register_schema(name, "jsonschema", json.dumps(schema).encode())
                for kind, (name, schema) in SCHEMAS.items()
            }
            channels: dict[int, int] = {}
            encoders = {}
            for node in nodes:
                channels[node.node_pk] = writer.register_channel(
                    topic=node.topic,
                    message_encoding="json",
                    schema_id=schema_ids[node.kind],
                    metadata={
                        "equipment_id": node.equipment_id,
                        "equipment_name": node.equipment_name,
                        "node_id": node.node_id,
                        "node_name": node.node_name,
                        "data_type": node.data_type,
                        "unit": node.unit,
                    },
                )
                encoders[node.node_pk] = ENCODERS[node.kind]
            equipment = sorted({node.equipment_id for node in nodes})
            meta = {
                "database": db.name,
                "table": "manufacturing.opcua_sample",
                "time_column": "ts",
                "equipment": ",".join(equipment),
                "channels": str(len(nodes)),
            }
            if start is not None:
                meta["start"] = start.astimezone(timezone.utc).isoformat()
            if end is not None:
                meta["end"] = end.astimezone(timezone.utc).isoformat()
            writer.add_metadata("opcua_sample", meta)
            heap: list[tuple[int, int, int]] = []
            for index, stream in enumerate(streams):
                if stream.pull():
                    heapq.heappush(heap, (stream.ts_ms, stream.node.node_pk, index))
            next_report = 1_000_000
            while heap:
                _ts, node_pk, index = heapq.heappop(heap)
                stream = streams[index]
                log_time = stream.ts_ms * 1_000_000
                writer.add_message(
                    channel_id=channels[node_pk],
                    log_time=log_time,
                    publish_time=log_time,
                    data=encoders[node_pk](stream.value, stream.quality),
                )
                written += 1
                if written >= next_report:
                    stamp = datetime.fromtimestamp(stream.ts_ms / 1000, timezone.utc).isoformat()
                    elapsed = time.monotonic() - started
                    rate = written / elapsed if elapsed else 0
                    print(f"messages {written} ts {stamp} rate {rate:.0f}/s", flush=True)
                    next_report += 1_000_000
                if stream.pull():
                    heapq.heappush(heap, (stream.ts_ms, stream.node.node_pk, index))
            writer.finish()
        errors = [message for stream in streams if (message := stream_error(stream, err_dir))]
        if errors:
            raise RuntimeError("; ".join(errors))
        os.replace(partial, output)
    except BaseException:
        stop_streams(streams)
        partial.unlink(missing_ok=True)
        raise
    finally:
        for path in err_dir.glob("*.err"):
            path.unlink(missing_ok=True)
        err_dir.rmdir()
    verify(output, written)


def verify(output: Path, written: int) -> None:
    with output.open("rb") as handle:
        summary = make_reader(handle).get_summary()
    if summary is None or summary.statistics is None:
        raise RuntimeError("mcap summary missing")
    stats = summary.statistics
    if stats.message_count != written:
        raise RuntimeError(f"message count {stats.message_count} != written {written}")
    start = datetime.fromtimestamp(stats.message_start_time / 1e9, timezone.utc)
    end = datetime.fromtimestamp(stats.message_end_time / 1e9, timezone.utc)
    size = output.stat().st_size
    print(
        f"wrote {output} bytes {size} messages {stats.message_count} "
        f"channels {stats.channel_count} start {start.isoformat()} end {end.isoformat()}",
        flush=True,
    )


def parse_time(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export opcua_sample rows to MCAP.")
    parser.add_argument("--equipment", help="equipment_id to export. Default: every equipment that has samples.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit-per-node", type=int, help="Export only the first N samples of each node.")
    parser.add_argument("--start", help="Inclusive start timestamptz (ISO-8601).")
    parser.add_argument("--end", help="Exclusive end timestamptz (ISO-8601).")
    args = parser.parse_args()
    if args.limit_per_node is not None and args.limit_per_node < 1:
        parser.error("--limit-per-node must be >= 1")
    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    if start and end and end <= start:
        parser.error("--end must be after --start")
    try:
        db = db_from_env()
        nodes = load_nodes(db, args.equipment)
        if not nodes:
            print("no sampled nodes", file=sys.stderr)
            return 1
        for node in nodes:
            print(f"channel {node.topic} {node.data_type} {node.unit}".rstrip(), flush=True)
        if start or end:
            print(f"window start={start} end={end}", flush=True)
        export(db, nodes, args.output, args.limit_per_node, start, end)
    except Exception as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
