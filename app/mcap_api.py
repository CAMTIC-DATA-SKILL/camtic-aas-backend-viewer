"""로컬 MCAP 아카이브를 읽어 뷰어 탭에 구조를 보여 준다.

``.env`` 의 ``MCAP_ARCHIVE_DIR`` 아래 ``.mcap`` 만 고를 수 있다.
스키마·인코딩·채널·청크·메타데이터와, 토픽·시간으로 자른 부분 읽기를 제공한다.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from mcap.reader import make_reader
from pydantic import BaseModel, Field

from config import settings

router = APIRouter(prefix="/api/mcap", tags=["mcap"])

MAX_TOPICS = 8
MAX_POINTS = 5_000
MAX_RANGE_NS = 31 * 24 * 3600 * 1_000_000_000
DEFAULT_POINTS = 800
MAX_LIST = 500
MAX_CHUNKS = 200
MAX_MESSAGES = 200
SCHEMA_PREVIEW = 4_000


class SchemaInfo(BaseModel):
    id: int
    name: str
    encoding: str
    data_text: str
    data_bytes: int
    channel_count: int = 0


class ChannelInfo(BaseModel):
    id: int
    topic: str
    message_encoding: str
    schema_id: int = 0
    schema_name: Optional[str] = None
    schema_encoding: Optional[str] = None
    message_count: int = 0
    equipment_id: str = ""
    equipment_name: str = ""
    node_id: str = ""
    node_name: str = ""
    data_type: str = ""
    unit: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class ChunkInfo(BaseModel):
    index: int
    compression: str
    compressed_size: int
    uncompressed_size: int
    compression_ratio: Optional[float] = None
    start: datetime
    end: datetime
    channel_ids: list[int]
    topics: list[str]
    message_index_bytes: int = 0
    chunk_start_offset: int = 0


class MetadataInfo(BaseModel):
    name: str
    metadata: dict[str, str]


class McapFileInfo(BaseModel):
    name: str
    path: str
    size_bytes: int
    mtime: datetime


class McapArchiveStatus(BaseModel):
    enabled: bool
    archive_dir: Optional[str] = None
    archive_exists: bool = False
    file_count: int = 0
    selected: Optional[str] = None
    exists: bool = False
    size_bytes: Optional[int] = None
    channels: int = 0
    schemas: int = 0
    chunks: int = 0
    messages: int = 0
    metadata_count: int = 0
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    error: Optional[str] = None


class InspectResponse(BaseModel):
    file: str
    path: str
    size_bytes: int
    start: datetime
    end: datetime
    statistics: dict[str, Any]
    schemas: list[SchemaInfo]
    channels: list[ChannelInfo]
    metadata: list[MetadataInfo]
    chunk_count: int
    compression: list[str]


class ChunkListResponse(BaseModel):
    file: str
    start: datetime
    end: datetime
    topics: list[str] = Field(default_factory=list)
    total_chunks: int
    matched_chunks: int
    returned: int
    chunks: list[ChunkInfo]


class RawMessage(BaseModel):
    topic: str
    channel_id: int
    schema_name: Optional[str] = None
    message_encoding: str
    log_time: datetime
    publish_time: datetime
    sequence: int
    data_bytes: int
    payload: Any = None
    payload_text: Optional[str] = None


class MessageListResponse(BaseModel):
    file: str
    start: datetime
    end: datetime
    topics: list[str]
    limit: int
    returned: int
    truncated: bool
    elapsed_ms: float
    messages: list[RawMessage]
    note: str = "iter_messages(topics=..., starting_at=..., ending_before=...) 로 해당 토픽·구간만 읽었다."


class SamplePoint(BaseModel):
    ts: datetime
    value: Any = None
    quality: Optional[int] = None


class TopicSeries(BaseModel):
    topic: str
    unit: str = ""
    node_name: str = ""
    samples: list[SamplePoint]
    count_raw: int


class SeriesResponse(BaseModel):
    file: str
    start: datetime
    end: datetime
    points: int
    topics: list[str]
    overlapping_chunks: int
    elapsed_ms: float
    series: list[TopicSeries]
    note: str = "선택한 토픽만 시간 인덱스로 읽어 버킷 다운샘플했다."


@dataclass
class _SummaryCache:
    path: Path
    mtime_ns: int
    size: int
    start_ns: int
    end_ns: int
    message_count: int
    statistics: dict[str, Any]
    schemas: list[SchemaInfo]
    channels: list[ChannelInfo]
    metadata: list[MetadataInfo]
    chunks: list[ChunkInfo]
    topic_to_id: dict[str, int] = field(default_factory=dict)
    compression: list[str] = field(default_factory=list)


_CACHE: dict[str, _SummaryCache] = {}
_CACHE_LOCK = asyncio.Lock()


def _require_enabled() -> None:
    if not settings.mcap.enabled:
        raise HTTPException(status_code=503, detail="MCAP 탭이 꺼져 있습니다. MCAP_ENABLED=true 로 켜세요.")


def _archive_dir() -> Path:
    _require_enabled()
    archive = settings.mcap.archive_dir
    if archive is None:
        raise HTTPException(status_code=503, detail="MCAP_ARCHIVE_DIR 가 없습니다.")
    return archive.resolve()


def _safe_rel(name: str) -> str:
    raw = (name or "").strip().replace("\\", "/")
    parts = raw.split("/")
    if not raw or raw.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="잘못된 파일 이름입니다.")
    if not raw.lower().endswith(".mcap"):
        raise HTTPException(status_code=400, detail=".mcap 파일만 열 수 있습니다.")
    return raw


def _resolve_file(name: Optional[str]) -> tuple[str, Path]:
    archive = _archive_dir()
    if not archive.is_dir():
        raise HTTPException(status_code=404, detail=f"아카이브 폴더가 없습니다: {archive}")
    chosen = name.strip() if name and name.strip() else ""
    if not chosen:
        raise HTTPException(status_code=400, detail="file 파라미터가 필요합니다. 아카이브에서 파일을 고르세요.")
    rel = _safe_rel(chosen)
    path = (archive / rel).resolve()
    try:
        path.relative_to(archive)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="아카이브 밖 경로는 열 수 없습니다.") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"MCAP 파일이 없습니다: {rel}")
    return rel, path


def _ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc)


def _parse_time(value: datetime, label: str) -> int:
    if value.tzinfo is None:
        raise HTTPException(status_code=400, detail=f"{label} 에는 시간대가 필요합니다.")
    return int(value.astimezone(timezone.utc).timestamp() * 1e9)


def _schema_text(data: bytes, encoding: str) -> str:
    text = data.decode("utf-8", "replace")
    if encoding == "jsonschema":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    if len(text) > SCHEMA_PREVIEW:
        return text[:SCHEMA_PREVIEW] + "\n…(truncated)"
    return text


def _list_files_sync(archive: Path) -> list[McapFileInfo]:
    if not archive.is_dir():
        return []
    rows: list[McapFileInfo] = []
    for path in sorted(archive.rglob("*.mcap")):
        if not path.is_file():
            continue
        try:
            rel = str(path.resolve().relative_to(archive.resolve())).replace("\\", "/")
        except ValueError:
            continue
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        stat = path.stat()
        rows.append(
            McapFileInfo(
                name=rel,
                path=str(path.resolve()),
                size_bytes=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            )
        )
        if len(rows) >= MAX_LIST:
            break
    rows.sort(key=lambda item: item.mtime, reverse=True)
    return rows


def _read_summary(path: Path) -> _SummaryCache:
    if not path.is_file():
        raise FileNotFoundError(path)
    stat = path.stat()
    with path.open("rb") as handle:
        reader = make_reader(handle)
        summary = reader.get_summary()
        metadata = [MetadataInfo(name=item.name, metadata=dict(item.metadata)) for item in reader.iter_metadata()]
    if summary is None or summary.statistics is None:
        raise RuntimeError("MCAP summary 가 없습니다.")
    stats = summary.statistics
    id_to_topic = {cid: ch.topic for cid, ch in summary.channels.items()}
    schema_channel_counts: dict[int, int] = {}
    for channel in summary.channels.values():
        if channel.schema_id:
            schema_channel_counts[channel.schema_id] = schema_channel_counts.get(channel.schema_id, 0) + 1

    schemas = [
        SchemaInfo(
            id=schema.id,
            name=schema.name,
            encoding=schema.encoding,
            data_text=_schema_text(schema.data, schema.encoding),
            data_bytes=len(schema.data),
            channel_count=schema_channel_counts.get(schema.id, 0),
        )
        for schema in sorted(summary.schemas.values(), key=lambda item: item.id)
    ]

    channels: list[ChannelInfo] = []
    for channel_id, channel in sorted(summary.channels.items(), key=lambda item: item[1].topic):
        schema = summary.schemas.get(channel.schema_id) if channel.schema_id else None
        meta = dict(channel.metadata or {})
        channels.append(
            ChannelInfo(
                id=channel_id,
                topic=channel.topic,
                message_encoding=channel.message_encoding,
                schema_id=channel.schema_id or 0,
                schema_name=schema.name if schema else None,
                schema_encoding=schema.encoding if schema else None,
                message_count=int(stats.channel_message_counts.get(channel_id, 0)),
                equipment_id=meta.get("equipment_id", ""),
                equipment_name=meta.get("equipment_name", ""),
                node_id=meta.get("node_id", ""),
                node_name=meta.get("node_name", "") or channel.topic.rsplit("/", 1)[-1],
                data_type=meta.get("data_type", ""),
                unit=meta.get("unit", ""),
                metadata=meta,
            )
        )

    chunks: list[ChunkInfo] = []
    compressions: set[str] = set()
    for index, chunk in enumerate(summary.chunk_indexes or []):
        channel_ids = sorted(chunk.message_index_offsets.keys())
        ratio = None
        if chunk.compressed_size > 0:
            ratio = round(chunk.uncompressed_size / chunk.compressed_size, 2)
        compressions.add(chunk.compression or "none")
        chunks.append(
            ChunkInfo(
                index=index,
                compression=chunk.compression or "none",
                compressed_size=int(chunk.compressed_size),
                uncompressed_size=int(chunk.uncompressed_size),
                compression_ratio=ratio,
                start=_ns_to_dt(int(chunk.message_start_time)),
                end=_ns_to_dt(int(chunk.message_end_time)),
                channel_ids=channel_ids,
                topics=[id_to_topic[cid] for cid in channel_ids if cid in id_to_topic],
                message_index_bytes=int(chunk.message_index_length),
                chunk_start_offset=int(chunk.chunk_start_offset),
            )
        )

    return _SummaryCache(
        path=path.resolve(),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        start_ns=int(stats.message_start_time),
        end_ns=int(stats.message_end_time),
        message_count=int(stats.message_count),
        statistics={
            "message_count": int(stats.message_count),
            "channel_count": int(stats.channel_count),
            "schema_count": int(stats.schema_count),
            "chunk_count": int(stats.chunk_count),
            "metadata_count": int(stats.metadata_count),
            "attachment_count": int(stats.attachment_count),
            "message_start_time": int(stats.message_start_time),
            "message_end_time": int(stats.message_end_time),
        },
        schemas=schemas,
        channels=channels,
        metadata=metadata,
        chunks=chunks,
        topic_to_id={ch.topic: ch.id for ch in channels},
        compression=sorted(compressions),
    )


async def _summary(file: Optional[str]) -> tuple[str, _SummaryCache]:
    rel, path = _resolve_file(file)
    key = str(path)
    async with _CACHE_LOCK:
        try:
            stat = await asyncio.to_thread(path.stat)
        except FileNotFoundError as exc:
            _CACHE.pop(key, None)
            raise HTTPException(status_code=404, detail=f"MCAP 파일이 없습니다: {rel}") from exc
        cached = _CACHE.get(key)
        if cached is not None and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
            return rel, cached
        try:
            summary = await asyncio.to_thread(_read_summary, path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"MCAP을 읽지 못했습니다: {type(exc).__name__}") from exc
        _CACHE[key] = summary
        return rel, summary


def _resolve_window(
    summary: _SummaryCache,
    start: Optional[datetime],
    end: Optional[datetime],
    range_s: int,
) -> tuple[int, int]:
    if end is None:
        end_ns = summary.end_ns
    else:
        end_ns = _parse_time(end, "end")
    if start is None:
        start_ns = max(summary.start_ns, end_ns - range_s * 1_000_000_000)
    else:
        start_ns = _parse_time(start, "start")
    if start_ns > end_ns:
        raise HTTPException(status_code=400, detail="start 가 end 보다 늦습니다.")
    if end_ns - start_ns > MAX_RANGE_NS:
        raise HTTPException(status_code=400, detail="조회 기간은 31일까지입니다.")
    return max(start_ns, summary.start_ns), min(end_ns, summary.end_ns)


def _validate_topics(summary: _SummaryCache, topic: list[str]) -> list[str]:
    topics = list(dict.fromkeys(topic))
    if not topics:
        raise HTTPException(status_code=400, detail="topic 이 필요합니다.")
    if len(topics) > MAX_TOPICS:
        raise HTTPException(status_code=400, detail=f"topic 은 최대 {MAX_TOPICS}개입니다.")
    known = {item.topic for item in summary.channels}
    unknown = [name for name in topics if name not in known]
    if unknown:
        raise HTTPException(status_code=404, detail=f"없는 topic: {', '.join(unknown)}")
    return topics


def _overlapping_chunks(summary: _SummaryCache, start_ns: int, end_ns: int, topics: list[str]) -> list[ChunkInfo]:
    wanted = {summary.topic_to_id[topic] for topic in topics if topic in summary.topic_to_id}
    start_dt = _ns_to_dt(start_ns)
    end_dt = _ns_to_dt(end_ns)
    matched: list[ChunkInfo] = []
    for chunk in summary.chunks:
        if chunk.end < start_dt or chunk.start > end_dt:
            continue
        if wanted and not wanted.intersection(chunk.channel_ids):
            continue
        matched.append(chunk)
    return matched


def _decode(data: bytes) -> tuple[Any, Optional[int]]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return data.decode("utf-8", "replace"), None
    if isinstance(payload, dict) and "value" in payload:
        quality = payload.get("quality")
        return payload.get("value"), int(quality) if isinstance(quality, (int, float)) else None
    return payload, None


def _payload_preview(data: bytes, encoding: str) -> tuple[Any, Optional[str]]:
    if encoding == "json":
        try:
            return json.loads(data), None
        except json.JSONDecodeError:
            text = data.decode("utf-8", "replace")
            return None, text[:500]
    text = data.decode("utf-8", "replace")
    return None, text[:500] if text else None


@dataclass
class _Bucket:
    sum: float = 0.0
    count: int = 0
    last_ts: int = 0
    last_value: Any = None
    last_quality: Optional[int] = None
    numeric: bool = True

    def add(self, ts: int, value: Any, quality: Optional[int]) -> None:
        self.last_ts = ts
        self.last_value = value
        self.last_quality = quality
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            self.numeric = False
            return
        self.sum += float(value)
        self.count += 1

    def point(self) -> Optional[SamplePoint]:
        if self.last_ts <= 0:
            return None
        if self.numeric and self.count:
            value: Any = self.sum / self.count
        else:
            value = self.last_value
        return SamplePoint(ts=_ns_to_dt(self.last_ts), value=value, quality=self.last_quality)


def _read_series(
    path: Path,
    topics: list[str],
    start_ns: int,
    end_ns: int,
    points: int,
    meta_by_topic: dict[str, ChannelInfo],
) -> list[TopicSeries]:
    span = max(1, end_ns - start_ns)
    bucket_ns = span / points
    buckets: dict[str, list[Optional[_Bucket]]] = {topic: [None] * points for topic in topics}
    raw_counts = {topic: 0 for topic in topics}
    with path.open("rb") as handle:
        reader = make_reader(handle)
        for _schema, channel, message in reader.iter_messages(
            topics=topics,
            starting_at=start_ns,
            ending_before=end_ns + 1,
        ):
            topic = channel.topic
            raw_counts[topic] = raw_counts.get(topic, 0) + 1
            idx = int((message.log_time - start_ns) / bucket_ns)
            if idx < 0:
                continue
            if idx >= points:
                idx = points - 1
            slot = buckets[topic][idx]
            if slot is None:
                slot = _Bucket()
                buckets[topic][idx] = slot
            value, quality = _decode(message.data)
            slot.add(message.log_time, value, quality)
    series: list[TopicSeries] = []
    for topic in topics:
        info = meta_by_topic.get(topic)
        samples = [point for bucket in buckets[topic] if bucket and (point := bucket.point())]
        series.append(
            TopicSeries(
                topic=topic,
                unit=info.unit if info else "",
                node_name=info.node_name if info else topic.rsplit("/", 1)[-1],
                samples=samples,
                count_raw=raw_counts.get(topic, 0),
            )
        )
    return series


def _read_messages(
    path: Path,
    topics: list[str],
    start_ns: int,
    end_ns: int,
    limit: int,
) -> tuple[list[RawMessage], bool]:
    rows: list[RawMessage] = []
    truncated = False
    with path.open("rb") as handle:
        reader = make_reader(handle)
        for schema, channel, message in reader.iter_messages(
            topics=topics,
            starting_at=start_ns,
            ending_before=end_ns + 1,
        ):
            if len(rows) >= limit:
                truncated = True
                break
            payload, text = _payload_preview(message.data, channel.message_encoding)
            rows.append(
                RawMessage(
                    topic=channel.topic,
                    channel_id=channel.id,
                    schema_name=schema.name if schema else None,
                    message_encoding=channel.message_encoding,
                    log_time=_ns_to_dt(message.log_time),
                    publish_time=_ns_to_dt(message.publish_time),
                    sequence=message.sequence,
                    data_bytes=len(message.data),
                    payload=payload,
                    payload_text=text,
                )
            )
    return rows, truncated


@router.get("/files", response_model=list[McapFileInfo])
async def mcap_files() -> list[McapFileInfo]:
    archive = _archive_dir()
    return await asyncio.to_thread(_list_files_sync, archive)


@router.get("/status", response_model=McapArchiveStatus)
async def mcap_status(file: Optional[str] = Query(None, description="아카이브 안 상대 경로. 없으면 목록만.")) -> McapArchiveStatus:
    cfg = settings.mcap
    if not cfg.enabled:
        return McapArchiveStatus(
            enabled=False,
            archive_dir=str(cfg.archive_dir) if cfg.archive_dir else None,
            error="MCAP_ENABLED=false",
        )
    if cfg.archive_dir is None:
        return McapArchiveStatus(enabled=True, error="MCAP_ARCHIVE_DIR 가 없습니다.")
    archive = cfg.archive_dir.resolve()
    if not archive.is_dir():
        return McapArchiveStatus(
            enabled=True,
            archive_dir=str(archive),
            archive_exists=False,
            error="아카이브 폴더가 없습니다.",
        )
    files = await asyncio.to_thread(_list_files_sync, archive)
    selected = file.strip() if file and file.strip() else None
    base = McapArchiveStatus(
        enabled=True,
        archive_dir=str(archive),
        archive_exists=True,
        file_count=len(files),
        selected=selected,
    )
    if not files:
        base.error = "아카이브에 .mcap 파일이 없습니다."
        return base
    if not selected:
        return base
    try:
        rel, summary = await _summary(selected)
    except HTTPException as exc:
        base.exists = False
        base.selected = selected
        base.error = str(exc.detail)
        return base
    return base.model_copy(
        update={
            "selected": rel,
            "exists": True,
            "size_bytes": summary.size,
            "channels": len(summary.channels),
            "schemas": len(summary.schemas),
            "chunks": len(summary.chunks),
            "messages": summary.message_count,
            "metadata_count": len(summary.metadata),
            "start": _ns_to_dt(summary.start_ns),
            "end": _ns_to_dt(summary.end_ns),
        }
    )


@router.get("/inspect", response_model=InspectResponse)
async def mcap_inspect(file: str = Query(..., description="아카이브 안 상대 경로")) -> InspectResponse:
    rel, summary = await _summary(file)
    return InspectResponse(
        file=rel,
        path=str(summary.path),
        size_bytes=summary.size,
        start=_ns_to_dt(summary.start_ns),
        end=_ns_to_dt(summary.end_ns),
        statistics=summary.statistics,
        schemas=summary.schemas,
        channels=summary.channels,
        metadata=summary.metadata,
        chunk_count=len(summary.chunks),
        compression=summary.compression,
    )


@router.get("/channels", response_model=list[ChannelInfo])
async def mcap_channels(file: Optional[str] = Query(None, description="아카이브 안 상대 경로")) -> list[ChannelInfo]:
    return (await _summary(file))[1].channels


@router.get("/chunks", response_model=ChunkListResponse)
async def mcap_chunks(
    file: str = Query(...),
    topic: list[str] = Query(default=[]),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    range_s: int = Query(300, ge=1, le=31 * 24 * 3600),
    limit: int = Query(50, ge=1, le=MAX_CHUNKS),
    offset: int = Query(0, ge=0),
) -> ChunkListResponse:
    rel, summary = await _summary(file)
    topics = list(dict.fromkeys(topic))
    if topics:
        topics = _validate_topics(summary, topics)
    start_ns, end_ns = _resolve_window(summary, start, end, range_s)
    matched = _overlapping_chunks(summary, start_ns, end_ns, topics)
    page = matched[offset: offset + limit]
    return ChunkListResponse(
        file=rel,
        start=_ns_to_dt(start_ns),
        end=_ns_to_dt(end_ns),
        topics=topics,
        total_chunks=len(summary.chunks),
        matched_chunks=len(matched),
        returned=len(page),
        chunks=page,
    )


@router.get("/messages", response_model=MessageListResponse)
async def mcap_messages(
    file: str = Query(...),
    topic: list[str] = Query(..., min_length=1),
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    range_s: int = Query(60, ge=1, le=31 * 24 * 3600),
    limit: int = Query(50, ge=1, le=MAX_MESSAGES),
) -> MessageListResponse:
    rel, summary = await _summary(file)
    topics = _validate_topics(summary, topic)
    start_ns, end_ns = _resolve_window(summary, start, end, range_s)
    started = time.perf_counter()
    rows, truncated = await asyncio.to_thread(_read_messages, summary.path, topics, start_ns, end_ns, limit)
    return MessageListResponse(
        file=rel,
        start=_ns_to_dt(start_ns),
        end=_ns_to_dt(end_ns),
        topics=topics,
        limit=limit,
        returned=len(rows),
        truncated=truncated,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        messages=rows,
    )


@router.get("/series", response_model=SeriesResponse)
async def mcap_series(
    topic: list[str] = Query(..., min_length=1, description="채널 topic. 최대 8개"),
    file: Optional[str] = Query(None, description="아카이브 안 상대 경로"),
    start: Optional[datetime] = Query(None, description="UTC 시작. 없으면 파일 끝에서 range_s 만큼"),
    end: Optional[datetime] = Query(None, description="UTC 끝. 없으면 파일 끝"),
    range_s: int = Query(300, ge=1, le=31 * 24 * 3600, description="start/end 없을 때 최근 N초"),
    points: int = Query(DEFAULT_POINTS, ge=10, le=MAX_POINTS),
) -> SeriesResponse:
    rel, summary = await _summary(file)
    topics = _validate_topics(summary, topic)
    start_ns, end_ns = _resolve_window(summary, start, end, range_s)
    known = {item.topic: item for item in summary.channels}
    started = time.perf_counter()
    series = await asyncio.to_thread(_read_series, summary.path, topics, start_ns, end_ns, points, known)
    overlapped = _overlapping_chunks(summary, start_ns, end_ns, topics)
    return SeriesResponse(
        file=rel,
        start=_ns_to_dt(start_ns),
        end=_ns_to_dt(end_ns),
        points=points,
        topics=topics,
        overlapping_chunks=len(overlapped),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        series=series,
    )
