"""ORM 模型。

设计要点：
- ``submissions`` 保存每次提交/复核的元数据与校验报告（幂等重放所需）；
  ``kind`` 区分普通提交 (SUBMISSION) 与质控复核 (REVIEW)，复核行额外记录
  来源批号、豁免行集合与原始载荷快照；
- 只有校验通过的批次才会写入 ``batches`` 与 ``batch_records``，
  因此校验失败的批次不会留下任何可供下游读取的登记数据。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Submission(Base):
    __tablename__ = "submissions"

    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # SUBMISSION=普通提交；REVIEW=质控复核（新批号，溯源到 source_batch_id）
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="SUBMISSION", index=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 原始请求载荷的规范化 JSON，复核时用于与请求中的原始记录逐字节比对
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # PASSED / FAILED
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    rule_set_json: Mapped[str] = mapped_column(Text, nullable=False)
    # 仅复核行使用：来源批号与已批准豁免的原行号
    source_batch_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("submissions.batch_id"), nullable=True, index=True
    )
    exempted_indexes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    batch: Mapped["Batch | None"] = relationship(
        back_populates="submission", cascade="all, delete-orphan", uselist=False
    )


class Batch(Base):
    """仅登记校验通过的批次版本，供下游读取。"""

    __tablename__ = "batches"

    batch_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("submissions.batch_id"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    submission: Mapped[Submission] = relationship(back_populates="batch")
    records: Mapped[list["BatchRecord"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="BatchRecord.record_index"
    )


class BatchRecord(Base):
    __tablename__ = "batch_records"
    __table_args__ = (UniqueConstraint("batch_id", "record_index", name="uq_batch_record_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("batches.batch_id", ondelete="CASCADE"), nullable=False, index=True
    )
    record_index: Mapped[int] = mapped_column(Integer, nullable=False)
    record_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    batch: Mapped[Batch] = relationship(back_populates="records")
