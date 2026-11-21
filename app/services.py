"""业务服务层：幂等提交、质控复核、批次登记与查询。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import canonical_json
from .models import Batch, BatchRecord, Submission
from .schemas import (
    BatchSummaryOut,
    RegisteredBatchOut,
    BatchRecordOut,
    ReviewMeta,
    ReviewRequest,
    SubmissionRequest,
    SubmissionResponse,
    ValidationReport,
)
from .validation import (
    EXEMPT_FIELD,
    EXEMPT_RULE,
    is_exemptable_min_failure,
    validate_batch,
    validate_batch_review,
)

SUBMISSION_KIND = "SUBMISSION"
REVIEW_KIND = "REVIEW"


class IdempotencyConflict(Exception):
    """同一 batch_id 重复提交但请求体与首次不一致。"""


class ReviewError(Exception):
    """复核前置条件不满足（HTTP 4xx，由路由层映射状态码）。"""

    status_code = 422

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ReviewNotFound(ReviewError):
    """原失败批号不存在或本身不是普通提交。"""

    status_code = 404


class ReviewConflict(ReviewError):
    """新批号已被占用且复核内容不一致（普通提交撞号或复核内容不同）。"""

    status_code = 409


def _payload_body(
    schema_version: str, records: list[dict], rules_dump: dict
) -> dict:
    return {
        "schema_version": schema_version,
        "records": records,
        "rules": rules_dump,
    }


def _hash_payload(req: SubmissionRequest) -> str:
    return _hash_parts(req.schema_version, req.records, req.rules.model_dump())


def _hash_review(req: ReviewRequest) -> str:
    return _hash_parts(req.schema_version, req.records, req.rules.model_dump())


def _hash_parts(schema_version: str, records: list[dict], rules_dump: dict) -> str:
    body = _payload_body(schema_version, records, rules_dump)
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 普通提交
# ---------------------------------------------------------------------------


def submit_batch(db: Session, req: SubmissionRequest) -> SubmissionResponse:
    """提交批次；同 batch_id + 同请求体的重复调用返回完全一致的结果。"""
    payload_hash = _hash_payload(req)

    existing = db.get(Submission, req.batch_id)
    if existing is not None:
        # 普通提交与复核撞号：无论内容是否一致都拒绝（409 由路由层映射）
        if existing.kind != SUBMISSION_KIND:
            raise IdempotencyConflict(req.batch_id)
        if existing.payload_hash != payload_hash:
            raise IdempotencyConflict(req.batch_id)
        # 幂等重放：直接返回首次生成的报告，不重新登记
        return _replay(existing)

    # 并发下可能有多个线程同时进入此处；flush/commit 都可能触发 UNIQUE 冲突，
    # 冲突后回滚并重放先提交者的结果，最多重试 3 次（SQLite busy_timeout 之外的兜底）
    for attempt in range(3):
        try:
            return _persist_new_submission(db, req, payload_hash)
        except IntegrityError:
            db.rollback()
            existing = db.get(Submission, req.batch_id)
            if existing is None:
                # 极端情况下先提交者的事务仍未可见，短暂等待后重试
                if attempt < 2:
                    continue
                raise
            if existing.kind != SUBMISSION_KIND or existing.payload_hash != payload_hash:
                raise IdempotencyConflict(req.batch_id)
            return _replay(existing)
    raise RuntimeError("unreachable")  # pragma: no cover


def _replay(existing: Submission) -> SubmissionResponse:
    report = ValidationReport.model_validate_json(existing.report_json)
    return SubmissionResponse(
        batch_id=existing.batch_id,
        schema_version=existing.schema_version,
        kind=existing.kind,
        status=existing.status,
        replayed=True,
        registered=existing.batch is not None,
        report=report,
        review=_review_meta(existing),
    )


def _persist_new_submission(
    db: Session, req: SubmissionRequest, payload_hash: str
) -> SubmissionResponse:
    report = validate_batch(req.batch_id, req.schema_version, req.records, req.rules)
    rules_dump = req.rules.model_dump()

    submission = Submission(
        batch_id=req.batch_id,
        kind=SUBMISSION_KIND,
        schema_version=req.schema_version,
        payload_hash=payload_hash,
        payload_json=canonical_json(
            _payload_body(req.schema_version, req.records, rules_dump)
        ),
        status=report.status,
        record_count=len(req.records),
        report_json=report.model_dump_json(),
        rule_set_json=canonical_json(rules_dump),
    )
    db.add(submission)

    # 关键不变量：只有 PASSED 才登记可读批次，FAILED 不写任何批次数据
    if report.status == "PASSED":
        _register_batch(db, req.batch_id, req.schema_version, req.records, payload_hash)

    db.commit()

    return SubmissionResponse(
        batch_id=req.batch_id,
        schema_version=req.schema_version,
        kind=SUBMISSION_KIND,
        status=report.status,
        replayed=False,
        registered=report.status == "PASSED",
        report=report,
        review=None,
    )


# ---------------------------------------------------------------------------
# 质控复核
# ---------------------------------------------------------------------------


def review_batch(db: Session, req: ReviewRequest) -> SubmissionResponse:
    """对失败批次执行带豁免的复核，以新批号保存可查询报告。

    前置条件（任一不满足即拒绝，不写入任何数据）：
    1. 来源批号存在且为普通提交，并且状态为 FAILED；
    2. 请求中的原始记录与来源批次首次提交的记录逐字节等价（schema/rules 同理）；
    3. 每个豁免行号在界内，且原报告中该行确实存在 amount_cents 整数 0 的
       min 下界失败。

    幂等：新批号 + 相同来源与豁免集合（等价地，相同请求体）可重放；
    新批号被内容不同的提交/复核占用时返回 409。
    """
    source = db.get(Submission, req.source_batch_id)
    if source is None or source.kind != SUBMISSION_KIND:
        raise ReviewNotFound(
            f"来源批次 {req.source_batch_id} 不存在或不是普通提交，无法复核"
        )
    if source.status != "FAILED":
        raise ReviewError(
            f"来源批次 {req.source_batch_id} 状态为 {source.status}，"
            "仅 FAILED 批次可以申请复核"
        )

    _assert_payload_matches(source, req)
    _assert_exemptions_eligible(source, req)

    # 用原报告中的失败明细做防御性校验后，再基于剩余错误重算
    exempted_indexes = req.exempted_record_indexes
    for attempt in range(3):
        existing = db.get(Submission, req.batch_id)
        if existing is not None:
            return _replay_or_conflict(existing, req)
        try:
            return _persist_new_review(db, req, exempted_indexes)
        except IntegrityError:
            db.rollback()
            existing = db.get(Submission, req.batch_id)
            if existing is None:
                if attempt < 2:
                    continue
                raise
            return _replay_or_conflict(existing, req)
    raise RuntimeError("unreachable")  # pragma: no cover


def _assert_payload_matches(source: Submission, req: ReviewRequest) -> None:
    """原始记录（连同 schema_version/rules）必须与首次提交完全一致。

    存储侧载荷经过 Pydantic 模型 dump（可选字段补 null），因此比较时双方
    都走一次模型规范化，避免“缺省字段 vs 显式 null”这类表示差异造成误判；
    records 内的实际数据仍按规范化 JSON 逐字节语义比对。
    """
    if source.payload_json is None:
        raise ReviewError(
            f"来源批次 {source.batch_id} 缺少原始载荷快照，无法核对原始记录"
        )
    original = json.loads(source.payload_json)
    original_req = SubmissionRequest.model_validate(
        {
            "batch_id": source.batch_id,
            "schema_version": original["schema_version"],
            "records": original["records"],
            "rules": original["rules"],
        }
    )
    if canonical_json(
        _payload_body(
            original_req.schema_version,
            original_req.records,
            original_req.rules.model_dump(),
        )
    ) != canonical_json(
        _payload_body(req.schema_version, req.records, req.rules.model_dump())
    ):
        raise ReviewError(
            "复核请求中的原始记录/schema_version/rules 与来源批次 "
            f"{source.batch_id} 的首次提交不一致，拒绝复核"
        )


def _assert_exemptions_eligible(source: Submission, req: ReviewRequest) -> None:
    """校验豁免行：界内 + 原报告存在 amount_cents=0（整数）的 min 失败。"""
    original = json.loads(source.payload_json)
    records: list[dict] = original["records"]
    report = ValidationReport.model_validate_json(source.report_json)
    failures_by_index = {rf.record_index: rf.field_failures for rf in report.record_failures}

    for idx in req.exempted_record_indexes:
        if idx < 0 or idx >= len(records):
            raise ReviewError(
                f"豁免行号 {idx} 越界：来源批次仅含 {len(records)} 条记录"
            )
        field_failures = failures_by_index.get(idx)
        if not field_failures:
            raise ReviewError(
                f"行 {idx} 在来源报告中没有任何字段失败，不满足豁免条件"
            )
        if not is_exemptable_min_failure(records[idx], field_failures):
            raise ReviewError(
                f"行 {idx} 不满足豁免条件：必须存在 {EXEMPT_FIELD} 为整数 0 "
                f"触发的 {EXEMPT_RULE} 下界失败"
            )


def _replay_or_conflict(
    existing: Submission, req: ReviewRequest
) -> SubmissionResponse:
    review_hash = _hash_review(req)
    same_content = existing.payload_hash == review_hash
    same_review = (
        existing.kind == REVIEW_KIND
        and existing.source_batch_id == req.source_batch_id
        and _stored_indexes(existing) == sorted(set(req.exempted_record_indexes))
    )
    if same_content and same_review:
        return _replay(existing)
    # 普通提交与复核撞号，或复核内容（来源/豁免行/载荷）不同 -> 409
    if existing.kind == SUBMISSION_KIND:
        detail = (
            f"新批号 {req.batch_id} 已被普通提交占用，复核不能复用该批号"
        )
    else:
        detail = (
            f"批号 {req.batch_id} 已存在复核记录，但本次的来源批号/豁免行集合/"
            "原始载荷与首次复核不一致，同一批号的复核请求必须保持一致"
        )
    raise ReviewConflict(detail)


def _stored_indexes(existing: Submission) -> list[int]:
    if not existing.exempted_indexes_json:
        return []
    return json.loads(existing.exempted_indexes_json)


def _persist_new_review(
    db: Session,
    req: ReviewRequest,
    exempted_indexes: list[int],
) -> SubmissionResponse:
    rules_dump = req.rules.model_dump()
    payload_hash = _hash_parts(req.schema_version, req.records, rules_dump)

    # 按剩余错误重算失败行与批次阈值
    report = validate_batch_review(
        batch_id=req.batch_id,
        schema_version=req.schema_version,
        records=req.records,
        rules=req.rules,
        source_batch_id=req.source_batch_id,
        exempted_indexes=exempted_indexes,
    )

    submission = Submission(
        batch_id=req.batch_id,
        kind=REVIEW_KIND,
        schema_version=req.schema_version,
        payload_hash=payload_hash,
        payload_json=canonical_json(
            _payload_body(req.schema_version, req.records, rules_dump)
        ),
        status=report.status,
        record_count=len(req.records),
        report_json=report.model_dump_json(),
        rule_set_json=canonical_json(rules_dump),
        source_batch_id=req.source_batch_id,
        exempted_indexes_json=canonical_json(sorted(set(exempted_indexes))),
    )
    db.add(submission)

    # 只有复核通过（剩余错误全部满足阈值）才登记整批原始记录供下游读取
    if report.status == "PASSED":
        _register_batch(db, req.batch_id, req.schema_version, req.records, payload_hash)

    db.commit()

    return SubmissionResponse(
        batch_id=req.batch_id,
        schema_version=req.schema_version,
        kind=REVIEW_KIND,
        status=report.status,
        replayed=False,
        registered=report.status == "PASSED",
        report=report,
        review=report.review,
    )


def _register_batch(
    db: Session,
    batch_id: str,
    schema_version: str,
    records: list[dict],
    payload_hash: str,
) -> None:
    batch = Batch(
        batch_id=batch_id,
        schema_version=schema_version,
        record_count=len(records),
        payload_hash=payload_hash,
    )
    db.add(batch)
    db.flush()  # 先建立批次，再写明细
    for idx, record in enumerate(records):
        db.add(
            BatchRecord(
                batch_id=batch_id,
                record_index=idx,
                record_json=record,
            )
        )


def _review_meta(existing: Submission) -> ReviewMeta | None:
    if existing.kind != REVIEW_KIND or not existing.source_batch_id:
        return None
    return ReviewMeta(
        source_batch_id=existing.source_batch_id,
        exempted_record_indexes=_stored_indexes(existing),
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def get_submission(db: Session, batch_id: str) -> SubmissionResponse | None:
    existing = db.get(Submission, batch_id)
    if existing is None:
        return None
    report = ValidationReport.model_validate_json(existing.report_json)
    return SubmissionResponse(
        batch_id=existing.batch_id,
        schema_version=existing.schema_version,
        kind=existing.kind,
        status=existing.status,
        replayed=True,
        registered=existing.batch is not None,
        report=report,
        review=_review_meta(existing),
    )


def get_registered_batch(db: Session, batch_id: str) -> RegisteredBatchOut | None:
    batch = db.get(Batch, batch_id)
    if batch is None:
        return None
    return RegisteredBatchOut(
        batch_id=batch.batch_id,
        schema_version=batch.schema_version,
        record_count=batch.record_count,
        registered_at=_iso(batch.registered_at),
        records=[
            BatchRecordOut(record_index=r.record_index, record=r.record_json)
            for r in batch.records
        ],
    )


def list_registered_batches(db: Session) -> list[BatchSummaryOut]:
    rows = db.scalars(select(Batch).order_by(Batch.registered_at.desc())).all()
    return [
        BatchSummaryOut(
            batch_id=b.batch_id,
            schema_version=b.schema_version,
            record_count=b.record_count,
            registered_at=_iso(b.registered_at),
        )
        for b in rows
    ]


def get_report(db: Session, batch_id: str) -> ValidationReport | None:
    existing = db.get(Submission, batch_id)
    if existing is None:
        return None
    return ValidationReport.model_validate_json(existing.report_json)


def ruleset_snapshot(db: Session, batch_id: str) -> dict | None:
    existing = db.get(Submission, batch_id)
    if existing is None:
        return None
    return json.loads(existing.rule_set_json)
