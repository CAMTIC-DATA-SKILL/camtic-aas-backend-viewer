"""기존 운영 테이블(schema manufacturing)의 쿼리 매핑. 스키마 생성·변경은 수행하지 않는다.

컬럼은 운영 DB에서 확인된 목록과 정확히 같아야 한다 (tests/manufacturing/test_contract.py 가 검사).
운영 목록에 없는 컬럼(예: equipment.created_at)을 여기에 넣으면 엔티티를 읽는 모든 조회가 실패할 수 있다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Double, ForeignKey, ForeignKeyConstraint, Identity, Integer, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .db import SCHEMA

TZ = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


class Equipment(Base):
    __tablename__ = "equipment"
    __table_args__ = {"schema": SCHEMA}

    equipment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    equipment_type: Mapped[str] = mapped_column(Text)
    equipment_name: Mapped[str] = mapped_column(Text)
    manufacturer: Mapped[Optional[str]] = mapped_column(Text)
    model_name: Mapped[Optional[str]] = mapped_column(Text)
    serial_number: Mapped[Optional[str]] = mapped_column(Text)
    aas_id: Mapped[Optional[str]] = mapped_column(Text)
    opcua_endpoint: Mapped[Optional[str]] = mapped_column(Text)
    data_source: Mapped[str] = mapped_column(Text, server_default="REAL")  # 이 앱은 REAL 설비만 저장한다
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")


class EquipmentNode(Base):
    __tablename__ = "equipment_node"
    __table_args__ = {"schema": SCHEMA}

    equipment_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.equipment.equipment_id"), primary_key=True)
    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # GENERATED ALWAYS AS IDENTITY: INSERT 할 때 넣지 않는다.
    node_pk: Mapped[int] = mapped_column(Integer, Identity(always=True), unique=True)
    node_name: Mapped[str] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(Text)
    unit: Mapped[Optional[str]] = mapped_column(Text)
    sampling_ms: Mapped[int] = mapped_column(Integer, server_default="100")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")


class AasNodeMapping(Base):
    __tablename__ = "aas_node_mapping"
    __table_args__ = (
        ForeignKeyConstraint(
            ["equipment_id", "node_id"],
            [f"{SCHEMA}.equipment_node.equipment_id", f"{SCHEMA}.equipment_node.node_id"],
            ondelete="CASCADE",
        ),
        {"schema": SCHEMA},
    )

    mapping_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    equipment_id: Mapped[str] = mapped_column(Text)
    node_id: Mapped[str] = mapped_column(Text)
    aas_submodel_id: Mapped[str] = mapped_column(Text)
    aas_id_short_path: Mapped[str] = mapped_column(Text)
    mapping_type: Mapped[str] = mapped_column(Text, server_default="CURRENT_VALUE")


class OpcuaSample(Base):
    """하이퍼테이블. 대량 INSERT 는 ingest 서비스가 Core 문장으로 한다."""

    __tablename__ = "opcua_sample"
    __table_args__ = {"schema": SCHEMA}

    ts: Mapped[datetime] = mapped_column(TZ, primary_key=True)
    node_pk: Mapped[int] = mapped_column(Integer, ForeignKey(f"{SCHEMA}.equipment_node.node_pk"), primary_key=True)
    value_num: Mapped[Optional[float]] = mapped_column(Double)
    value_bool: Mapped[Optional[bool]] = mapped_column(Boolean)
    value_text: Mapped[Optional[str]] = mapped_column(Text)
    quality: Mapped[int] = mapped_column(BigInteger, server_default="0")
    source_ts: Mapped[Optional[datetime]] = mapped_column(TZ)
    received_at: Mapped[datetime] = mapped_column(TZ)


class EquipmentLatest(Base):
    __tablename__ = "equipment_latest"
    __table_args__ = (
        ForeignKeyConstraint(
            ["equipment_id", "node_id"],
            [f"{SCHEMA}.equipment_node.equipment_id", f"{SCHEMA}.equipment_node.node_id"],
            ondelete="CASCADE",
        ),
        {"schema": SCHEMA},
    )

    equipment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    node_pk: Mapped[int] = mapped_column(Integer, ForeignKey(f"{SCHEMA}.equipment_node.node_pk"), unique=True)
    last_ts: Mapped[datetime] = mapped_column(TZ)
    value_num: Mapped[Optional[float]] = mapped_column(Double)
    value_bool: Mapped[Optional[bool]] = mapped_column(Boolean)
    value_text: Mapped[Optional[str]] = mapped_column(Text)
    quality: Mapped[int] = mapped_column(BigInteger)
    source_ts: Mapped[Optional[datetime]] = mapped_column(TZ)
    received_at: Mapped[datetime] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default="now()")


class AlarmEvent(Base):
    __tablename__ = "alarm_event"
    __table_args__ = {"schema": SCHEMA}

    alarm_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    equipment_id: Mapped[str] = mapped_column(Text, ForeignKey(f"{SCHEMA}.equipment.equipment_id"))
    node_id: Mapped[Optional[str]] = mapped_column(Text)
    alarm_type: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TZ)
    ended_at: Mapped[Optional[datetime]] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(Text, server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(TZ, server_default="now()")
