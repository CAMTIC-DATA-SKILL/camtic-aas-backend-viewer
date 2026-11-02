"""/api/v1/timeseries 요청·응답 스키마 (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from .values import QUALITY_MAX

MAX_INGEST_SAMPLES = 10_000

# bool 이 int 로, 숫자 문자열이 숫자로 바뀌지 않게 Strict 타입만 받는다.
RawValue = Union[StrictBool, StrictInt, StrictFloat, StrictStr, None]


class ErrorBody(BaseModel):
    code: str
    message: str


class IngestSample(BaseModel):
    equipment_id: str = Field(min_length=1, max_length=64)
    node_id: str = Field(min_length=1, max_length=512)
    value: RawValue = None
    quality: int = Field(0, ge=0, le=QUALITY_MAX, description="OPC-UA StatusCode 원시값 (0 = Good)")
    source_ts: Optional[datetime] = Field(None, description="장비/서버 시각 (시간대 필수). 없으면 received_at 사용")
    received_at: Optional[datetime] = Field(None, description="수집 시스템 수신 시각. 없으면 API 수신 시각")


class IngestRequest(BaseModel):
    collector_id: Optional[str] = Field(None, max_length=64)
    data_source: Literal["REAL"] = Field(
        "REAL", description="실제 설비 데이터만 저장한다")
    samples: list[IngestSample] = Field(min_length=1, max_length=MAX_INGEST_SAMPLES)


class IngestRejected(BaseModel):
    index: int
    equipment_id: str
    node_id: str
    reason: str


class IngestResult(BaseModel):
    received: int
    inserted: int = Field(description="새로 저장된 행")
    duplicates: int = Field(description="이미 저장된 (node, ts) 라서 건너뜀")
    late: int = Field(description="최신값보다 과거 시각 (저장은 하지만 최신값은 덮어쓰지 않음)")
    bad_quality: int = Field(description="quality 가 Bad 인 샘플 (저장함)")
    rejected: int
    rejected_samples: list[IngestRejected] = Field(description="최대 100건")


class SamplePoint(BaseModel):
    ts: datetime
    value: RawValue
    quality: int
    source_ts: Optional[datetime]
    received_at: datetime


class BucketPoint(BaseModel):
    bucket: datetime
    avg: Optional[float]
    min: Optional[float]
    max: Optional[float]
    last: RawValue
    count: int
    bad_count: int


class NodeSeries(BaseModel):
    node_id: str
    node_name: str
    data_type: str
    unit: Optional[str]
    samples: Optional[list[SamplePoint]] = None
    buckets: Optional[list[BucketPoint]] = None
    summary: dict = Field(description="기간 전체 avg/min/max/count (숫자 노드)")


class HistoryResponse(BaseModel):
    equipment_id: str
    data_source: Optional[str] = Field(None, description="이 설비 데이터의 출처 (REAL)")
    start: datetime
    end: datetime
    mode: Literal["raw", "bucket"]
    bucket_seconds: Optional[float]
    limit: Optional[int]
    next_cursor: Optional[str] = Field(None, description="raw 모드에서 다음 페이지 (같은 조건 + cursor)")
    series: list[NodeSeries]
