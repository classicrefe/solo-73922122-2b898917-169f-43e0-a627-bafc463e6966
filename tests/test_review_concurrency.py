"""复核并发竞态自测：同一新批号并发复核，必须只登记一次且结果一致。"""
from __future__ import annotations

import json
import os
import tempfile
import threading

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="dqg-review-conc-"), "conc.db")
os.environ["DQG_DB_PATH"] = _TMP_DB

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Batch, BatchRecord  # noqa: E402
from app.schemas import ReviewRequest, SubmissionRequest  # noqa: E402
from app.services import review_batch, submit_batch  # noqa: E402

init_db()

RULES = {
    "field_rules": {
        "order_id": [{"type": "required"}, {"type": "regex", "pattern": "^ORD-\\d{4}$"}],
        "amount_cents": [{"type": "type", "value": "integer"}, {"type": "min", "min": 1}],
    },
    "unique_rules": [{"fields": ["order_id"]}],
    "batch_rules": {"max_failure_rate": 0.0, "min_record_count": 1, "max_record_count": 100},
}
RECORDS = [
    {"order_id": "ORD-8001", "amount_cents": 100},
    {"order_id": "ORD-8002", "amount_cents": 0},
    {"order_id": "ORD-8003", "amount_cents": 200},
    {"order_id": "ORD-8004", "amount_cents": 0},
]
SOURCE, NEW = "conc-src-1", "conc-new-1"

# 先制造一个 FAILED 源批次
with SessionLocal() as db:
    submit_batch(
        db,
        SubmissionRequest.model_validate(
            {"batch_id": SOURCE, "schema_version": "v1", "records": RECORDS, "rules": RULES}
        ),
    )

review_body = {
    "source_batch_id": SOURCE,
    "batch_id": NEW,
    "schema_version": "v1",
    "records": RECORDS,
    "rules": RULES,
    "exempted_record_indexes": [1, 3],
}

results: list[object] = []
errors: list[Exception] = []


def worker() -> None:
    try:
        db = SessionLocal()
        try:
            results.append(review_batch(db, ReviewRequest.model_validate(review_body)))
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


threads = [threading.Thread(target=worker) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

assert not errors, errors
assert len(results) == 8
statuses = {r.status for r in results}
assert statuses == {"PASSED"}
kinds = {r.kind for r in results}
assert kinds == {"REVIEW"}
assert sum(1 for r in results if not r.replayed) == 1, "恰有一次真实复核登记"
assert sum(1 for r in results if r.replayed) == 7, "其余全部幂等重放"
reports = {r.report.model_dump_json() for r in results}
assert len(reports) == 1, "所有复核响应报告必须完全一致"
metas = {json.dumps(r.review.model_dump(), sort_keys=True) for r in results}
assert len(metas) == 1

with SessionLocal() as db:
    assert db.get(Batch, NEW) is not None
    assert db.get(Batch, SOURCE) is None, "源失败批次仍不得登记"
    assert db.query(BatchRecord).filter_by(batch_id=NEW).count() == len(RECORDS)

print("复核并发测试通过：8 线程同新批号复核 -> 1 次登记 + 7 次重放，明细无重复")
