"""质控复核 API 端到端自测。

覆盖需求要点：
- 仅 amount_cents 整数 0 的 min 下界失败可豁免，其他失败保留并重算；
- 原始记录与首次提交不符 -> 拒绝；豁免行不满足条件 -> 拒绝；
- 复核通过才登记整批原始记录，原拒收报告仍可查、原批次仍不可读；
- 相同新批号 + 相同来源/豁免集合可重放；撞号或内容不同 -> 409。

每个用例使用独立批号，避免用例间状态依赖。
"""
from __future__ import annotations

import copy
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="dqg-review-"), "review.db")
os.environ["DQG_DB_PATH"] = _TMP_DB

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Batch, BatchRecord, Submission  # noqa: E402

init_db()
client = TestClient(app)

# 规则：赠品金额 amount_cents 必须 >= 1，失败率阈值 0（任一失败整批拒收）
RULES = {
    "field_rules": {
        "order_id": [{"type": "required"}, {"type": "regex", "pattern": "^ORD-\\d{4}$"}],
        "amount_cents": [{"type": "type", "value": "integer"}, {"type": "min", "min": 1}],
    },
    "unique_rules": [{"fields": ["order_id"]}],
    "batch_rules": {"max_failure_rate": 0.0, "min_record_count": 1, "max_record_count": 100},
}

# 4 条记录：行 1、3 为赠品（amount_cents=0，整数），触发 min 失败 -> 失败率 50% 拒收
SCHEMA = "order-schema/v3.0"


def _records() -> list[dict]:
    return [
        {"order_id": "ORD-5001", "amount_cents": 19950},
        {"order_id": "ORD-5002", "amount_cents": 0},  # 赠品，可豁免
        {"order_id": "ORD-5003", "amount_cents": 4200},
        {"order_id": "ORD-5004", "amount_cents": 0},  # 赠品，可豁免
    ]


def _submit_source(batch_id: str, records=None, rules=None) -> dict:
    payload = {
        "batch_id": batch_id,
        "schema_version": SCHEMA,
        "records": records if records is not None else _records(),
        "rules": rules if rules is not None else RULES,
    }
    r = client.post("/batches/submit", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _review_payload(
    *,
    new_batch_id: str,
    source_batch_id: str,
    records=None,
    exempted=(1, 3),
    schema_version=SCHEMA,
    rules=None,
) -> dict:
    return {
        "source_batch_id": source_batch_id,
        "batch_id": new_batch_id,
        "schema_version": schema_version,
        "records": records if records is not None else _records(),
        "rules": rules if rules is not None else RULES,
        "exempted_record_indexes": list(exempted),
    }


@pytest.fixture()
def ids(request: pytest.FixtureRequest) -> tuple[str, str]:
    """每个用例独享的（源批号，新批号），按用例名派生，保证互不冲突。"""
    slug = request.node.name.replace("_", "-")
    return f"src-{slug}", f"new-{slug}"


def test_source_batch_fails_and_is_not_readable(ids: tuple[str, str]) -> None:
    src, _new = ids
    body = _submit_source(src)
    assert body["status"] == "FAILED"
    assert body["kind"] == "SUBMISSION"
    assert body["registered"] is False
    failed_indexes = {rf["record_index"] for rf in body["report"]["record_failures"]}
    assert failed_indexes == {1, 3}
    for rf in body["report"]["record_failures"]:
        rules = [f["rule"] for f in rf["field_failures"]["amount_cents"]]
        assert rules == ["min"]
    # 阈值失败：失败率 0.5 > 0
    assert {bf["rule"] for bf in body["report"]["batch_failures"]} == {"max_failure_rate"}
    # 原批次不可读、不登记
    assert client.get(f"/batches/{src}").status_code == 404
    # 原拒收报告仍可查
    r = client.get(f"/submissions/{src}/report")
    assert r.status_code == 200
    assert r.json()["status"] == "FAILED"


def test_review_happy_path_passes_and_registers_full_batch(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    r = client.post("/batches/review", json=_review_payload(new_batch_id=new, source_batch_id=src))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "REVIEW"
    assert body["status"] == "PASSED"
    assert body["registered"] is True
    assert body["replayed"] is False
    # 报告注明来源批号与豁免行
    assert body["review"] == {
        "kind": "REVIEW",
        "source_batch_id": src,
        "exempted_record_indexes": [1, 3],
    }
    assert body["report"]["review"] == body["review"]
    assert body["report"]["batch_id"] == new
    assert body["report"]["summary"]["failed_records"] == 0
    assert body["report"]["summary"]["field_failure_count"] == 0
    assert body["report"]["record_failures"] == []

    # 只有复核通过才登记整批原始记录（4 条，含赠品行）供下游读取
    r = client.get(f"/batches/{new}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["record_count"] == 4
    assert [rec["record"]["amount_cents"] for rec in detail["records"]] == [19950, 0, 4200, 0]
    listed = [b["batch_id"] for b in client.get("/batches").json()]
    assert new in listed and src not in listed

    # 原批次仍不可读、原拒收报告仍可查
    assert client.get(f"/batches/{src}").status_code == 404
    assert client.get(f"/submissions/{src}").json()["status"] == "FAILED"
    # 复核报告可用新批号查询
    r = client.get(f"/submissions/{new}/report")
    assert r.status_code == 200
    assert r.json()["review"]["source_batch_id"] == src


def test_review_is_idempotent_replay(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    p1 = _review_payload(new_batch_id=new, source_batch_id=src)
    r1 = client.post("/batches/review", json=p1)
    r2 = client.post("/batches/review", json=p1)
    assert r1.status_code == r2.status_code == 200
    b1, b2 = r1.json(), r2.json()
    assert b1["replayed"] is False
    assert b2["replayed"] is True
    assert b1["report"] == b2["report"]
    assert b2["kind"] == "REVIEW"
    # 明细未重复登记
    with SessionLocal() as db:
        assert db.query(BatchRecord).filter_by(batch_id=new).count() == 4
    # 豁免行集合顺序不同但内容相同，同样视为重放
    swapped = _review_payload(new_batch_id=new, source_batch_id=src, exempted=(3, 1))
    r3 = client.post("/batches/review", json=swapped)
    assert r3.status_code == 200
    assert r3.json()["replayed"] is True
    # 重复行号被去重后同样重放
    dup = _review_payload(new_batch_id=new, source_batch_id=src, exempted=(1, 1, 3, 3))
    r4 = client.post("/batches/review", json=dup)
    assert r4.status_code == 200
    assert r4.json()["replayed"] is True


def test_review_rejects_tampered_records(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    tampered = _review_payload(new_batch_id=new, source_batch_id=src)
    tampered["records"][0]["amount_cents"] = 99999
    r = client.post("/batches/review", json=tampered)
    assert r.status_code == 422
    assert "首次提交不一致" in r.json()["detail"]
    # 被拒绝的复核不产生任何提交/登记数据
    assert client.get(f"/submissions/{new}").status_code == 404
    assert client.get(f"/batches/{new}").status_code == 404


def test_review_rejects_tampered_rules_and_schema(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    bad_rules = _review_payload(
        new_batch_id=f"{new}-r", source_batch_id=src, rules=copy.deepcopy(RULES)
    )
    bad_rules["rules"]["batch_rules"]["max_failure_rate"] = 0.5
    r = client.post("/batches/review", json=bad_rules)
    assert r.status_code == 422

    bad_schema = _review_payload(
        new_batch_id=f"{new}-s", source_batch_id=src, schema_version="order-schema/v9.9"
    )
    r = client.post("/batches/review", json=bad_schema)
    assert r.status_code == 422


def test_review_rejects_exemption_without_zero_min_failure(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    # 行 0 没有失败，豁免它 -> 拒绝
    p = _review_payload(new_batch_id=f"{new}-1", source_batch_id=src, exempted=(0,))
    r = client.post("/batches/review", json=p)
    assert r.status_code == 422
    assert "行 0" in r.json()["detail"]

    # 行 2 是正常金额（4200），没有 min 失败 -> 拒绝
    p = _review_payload(new_batch_id=f"{new}-2", source_batch_id=src, exempted=(2,))
    r = client.post("/batches/review", json=p)
    assert r.status_code == 422

    # 越界行号 -> 拒绝
    p = _review_payload(new_batch_id=f"{new}-3", source_batch_id=src, exempted=(99,))
    r = client.post("/batches/review", json=p)
    assert r.status_code == 422
    assert "越界" in r.json()["detail"]


def test_float_zero_is_not_exemptable(ids: tuple[str, str]) -> None:
    """amount_cents=0.0（浮点）不是整数 0，不满足豁免条件。"""
    src, new = ids
    records = _records()
    records[1]["amount_cents"] = 0.0
    _submit_source(src, records=records)
    p = _review_payload(new_batch_id=new, source_batch_id=src, records=records, exempted=(1,))
    r = client.post("/batches/review", json=p)
    assert r.status_code == 422
    assert "整数 0" in r.json()["detail"]


def test_remaining_errors_are_kept_and_recomputed(ids: tuple[str, str]) -> None:
    """只豁免行 1：行 3 的 min 失败保留，复核仍 FAILED 且不登记。"""
    src, new = ids
    _submit_source(src)
    p = _review_payload(new_batch_id=new, source_batch_id=src, exempted=(1,))
    r = client.post("/batches/review", json=p)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "FAILED"
    assert body["registered"] is False
    # 剩余失败行重新计算：只剩行 3
    assert {rf["record_index"] for rf in body["report"]["record_failures"]} == {3}
    assert body["report"]["summary"]["failed_records"] == 1
    assert body["report"]["summary"]["failure_rate"] == 0.25
    assert {bf["rule"] for bf in body["report"]["batch_failures"]} == {"max_failure_rate"}
    # 复核报告可查，但批次不可读
    assert client.get(f"/submissions/{new}").status_code == 200
    assert client.get(f"/batches/{new}").status_code == 404
    with SessionLocal() as db:
        assert db.get(Batch, new) is None
    # 豁免信息仍记录在报告中
    assert body["report"]["review"]["exempted_record_indexes"] == [1]


def test_other_field_failures_survive_exemption(ids: tuple[str, str]) -> None:
    """豁免行上同时存在其他字段失败时，只剔除 amount_cents 的 min 失败。"""
    src, new = ids
    records = _records()
    # 行 1 同时有坏 order_id（regex 失败）和 amount_cents=0（min 失败）
    records[1]["order_id"] = "BAD-ID"
    _submit_source(src, records=records)
    p = _review_payload(
        new_batch_id=new, source_batch_id=src, records=records, exempted=(1, 3)
    )
    r = client.post("/batches/review", json=p)
    body = r.json()
    assert body["status"] == "FAILED"
    rf = next(x for x in body["report"]["record_failures"] if x["record_index"] == 1)
    # amount_cents 的 min 已豁免剔除，order_id 的 regex 失败保留
    assert set(rf["field_failures"].keys()) == {"order_id"}
    assert [f["rule"] for f in rf["field_failures"]["order_id"]] == ["regex"]


def test_unique_failures_survive_exemption(ids: tuple[str, str]) -> None:
    """唯一性冲突不属于豁免范围，复核时必须保留。"""
    src, new = ids
    records = _records()
    records[3]["order_id"] = "ORD-5002"  # 与行 1 重复，行 3 同时是赠品
    _submit_source(src, records=records)
    p = _review_payload(
        new_batch_id=new, source_batch_id=src, records=records, exempted=(1, 3)
    )
    r = client.post("/batches/review", json=p)
    body = r.json()
    assert body["status"] == "FAILED"
    assert len(body["report"]["unique_failures"]) == 1
    assert body["report"]["unique_failures"][0]["record_indexes"] == [1, 3]
    assert client.get(f"/batches/{new}").status_code == 404


def test_review_unknown_source_404(ids: tuple[str, str]) -> None:
    _src, new = ids
    p = _review_payload(new_batch_id=new, source_batch_id="does-not-exist")
    r = client.post("/batches/review", json=p)
    assert r.status_code == 404


def test_cannot_review_passed_batch(ids: tuple[str, str]) -> None:
    src, new = ids
    good = {
        "batch_id": src,
        "schema_version": SCHEMA,
        "records": [{"order_id": "ORD-6001", "amount_cents": 100}],
        "rules": RULES,
    }
    assert client.post("/batches/submit", json=good).json()["status"] == "PASSED"
    p = _review_payload(
        new_batch_id=new, source_batch_id=src, records=good["records"], exempted=()
    )
    r = client.post("/batches/review", json=p)
    assert r.status_code == 422
    assert "FAILED" in r.json()["detail"]


def test_review_conflicts_with_normal_submission_409(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    # 新批号先被普通提交占用 -> 复核撞号 409
    _submit_source(new)
    p = _review_payload(new_batch_id=new, source_batch_id=src)
    r = client.post("/batches/review", json=p)
    assert r.status_code == 409
    assert "普通提交占用" in r.json()["detail"]


def test_review_same_id_different_content_409(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    # 同一新批号，不同豁免行集合 -> 409
    p = _review_payload(new_batch_id=new, source_batch_id=src, exempted=(1, 3))
    assert client.post("/batches/review", json=p).status_code == 200
    p2 = _review_payload(new_batch_id=new, source_batch_id=src, exempted=(1,))
    r = client.post("/batches/review", json=p2)
    assert r.status_code == 409

    # 不同来源批号（载荷同源但 source 不同）-> 409
    src2 = f"{src}-b"
    _submit_source(src2)
    p3 = _review_payload(new_batch_id=f"{new}-x", source_batch_id=src2, exempted=(1, 3))
    assert client.post("/batches/review", json=p3).status_code == 200
    p4 = _review_payload(new_batch_id=f"{new}-x", source_batch_id=src, exempted=(1, 3))
    r = client.post("/batches/review", json=p4)
    assert r.status_code == 409


def test_normal_submit_conflicts_with_review_id_409(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    # 反向撞号：复核占用的批号再做普通提交 -> 409（即便载荷完全相同）
    assert (
        client.post(
            "/batches/review",
            json=_review_payload(new_batch_id=new, source_batch_id=src),
        ).status_code
        == 200
    )
    payload = {
        "batch_id": new,
        "schema_version": SCHEMA,
        "records": _records(),
        "rules": RULES,
    }
    r = client.post("/batches/submit", json=payload)
    assert r.status_code == 409


def test_review_request_validation_422(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    # 负行号被 pydantic 拒绝
    p = _review_payload(new_batch_id=new, source_batch_id=src, exempted=(-1,))
    r = client.post("/batches/review", json=p)
    assert r.status_code == 422
    # 缺少必填字段
    r = client.post("/batches/review", json={"source_batch_id": src})
    assert r.status_code == 422


def test_review_submission_kind_persisted(ids: tuple[str, str]) -> None:
    src, new = ids
    _submit_source(src)
    client.post("/batches/review", json=_review_payload(new_batch_id=new, source_batch_id=src))
    with SessionLocal() as db:
        row = db.get(Submission, new)
        assert row is not None
        assert row.kind == "REVIEW"
        assert row.source_batch_id == src
        src_row = db.get(Submission, src)
        assert src_row.kind == "SUBMISSION"
        assert src_row.batch is None  # 原失败批次没有登记
        assert row.batch is not None  # 复核通过批次已登记


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
