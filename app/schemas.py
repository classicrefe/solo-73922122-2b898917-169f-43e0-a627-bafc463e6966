"""请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------

FieldRuleType = Literal[
    "required",
    "type",
    "min",
    "max",
    "min_length",
    "max_length",
    "in",
    "regex",
]


class FieldRule(BaseModel):
    type: FieldRuleType
    # 规则参数；不同 type 使用不同参数，未使用的参数保持 None
    value: Any | None = Field(default=None, description="type 规则的目标类型: string/integer/number/boolean")
    min: float | None = Field(default=None, description="min 规则下界")
    max: float | None = Field(default=None, description="max 规则上界")
    min_length: int | None = None
    max_length: int | None = None
    values: list[Any] | None = Field(default=None, description="in 规则允许的取值集合")
    pattern: str | None = Field(default=None, description="regex 规则的正则表达式")


class UniqueRule(BaseModel):
    fields: list[str] = Field(min_length=1)


class BatchRule(BaseModel):
    max_failure_rate: float = Field(ge=0.0, le=1.0)
    min_record_count: int = Field(default=0, ge=0)
    max_record_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "BatchRule":
        if self.max_record_count is not None and self.max_record_count < self.min_record_count:
            raise ValueError("max_record_count 不能小于 min_record_count")
        return self


class RuleSet(BaseModel):
    field_rules: dict[str, list[FieldRule]] = Field(default_factory=dict)
    unique_rules: list[UniqueRule] = Field(default_factory=list)
    batch_rules: BatchRule


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


class SubmissionRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    records: list[dict[str, Any]] = Field(default_factory=list)
    rules: RuleSet


class ReviewRequest(BaseModel):
    """质控员复核请求：对原失败批次中的零金额下界失败申请豁免后重新校验。"""

    source_batch_id: str = Field(min_length=1, max_length=128, description="原失败批号")
    batch_id: str = Field(min_length=1, max_length=128, description="复核后的新批号")
    schema_version: str = Field(min_length=1, max_length=64)
    records: list[dict[str, Any]] = Field(
        default_factory=list, description="原始记录，必须与首次提交逐字节等价"
    )
    rules: RuleSet
    exempted_record_indexes: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list, description="已批准豁免的原行号列表"
    )

    @model_validator(mode="after")
    def _dedupe_indexes(self) -> "ReviewRequest":
        # 去重并保持稳定顺序；集合语义用于幂等键与豁免计算
        seen: set[int] = set()
        deduped: list[int] = []
        for idx in self.exempted_record_indexes:
            if idx not in seen:
                seen.add(idx)
                deduped.append(idx)
        self.exempted_record_indexes = deduped
        return self


# ---------------------------------------------------------------------------
# 校验报告
# ---------------------------------------------------------------------------


class FieldFailure(BaseModel):
    rule: str
    message: str
    value: Any | None = None


class RecordFailure(BaseModel):
    record_index: int
    field_failures: dict[str, list[FieldFailure]] = Field(default_factory=dict)


class UniqueFailure(BaseModel):
    fields: list[str]
    values: list[dict[str, Any]]
    record_indexes: list[int]
    message: str


class BatchFailure(BaseModel):
    rule: str
    message: str


class ReviewMeta(BaseModel):
    """复核报告的来源溯源信息；普通提交的报告不含该字段。

    被豁免的失败固定为 ``amount_cents`` 取值整数 0 时触发的 ``min`` 下界失败，
    因此这里只需注明来源批号与豁免行号集合。
    """

    kind: Literal["REVIEW"] = "REVIEW"
    source_batch_id: str
    exempted_record_indexes: list[int]


class ValidationSummary(BaseModel):
    total_records: int
    passed_records: int
    failed_records: int
    failure_rate: float
    field_failure_count: int
    unique_failure_count: int
    batch_failure_count: int


class ValidationReport(BaseModel):
    batch_id: str
    schema_version: str
    status: Literal["PASSED", "FAILED"]
    summary: ValidationSummary
    record_failures: list[RecordFailure] = Field(default_factory=list)
    unique_failures: list[UniqueFailure] = Field(default_factory=list)
    batch_failures: list[BatchFailure] = Field(default_factory=list)
    review: ReviewMeta | None = Field(
        default=None, description="复核产生的报告在此注明来源批号与豁免行；普通提交为 null"
    )


# ---------------------------------------------------------------------------
# 接口响应
# ---------------------------------------------------------------------------


class SubmissionResponse(BaseModel):
    batch_id: str
    schema_version: str
    kind: Literal["SUBMISSION", "REVIEW"] = Field(
        default="SUBMISSION", description="SUBMISSION=普通提交，REVIEW=质控复核"
    )
    status: Literal["PASSED", "FAILED"]
    replayed: bool = Field(description="是否为重复提交的幂等重放结果")
    registered: bool
    report: ValidationReport
    review: ReviewMeta | None = Field(
        default=None, description="复核请求的来源批号与豁免行；普通提交为 null"
    )


class BatchRecordOut(BaseModel):
    record_index: int
    record: dict[str, Any]


class RegisteredBatchOut(BaseModel):
    batch_id: str
    schema_version: str
    record_count: int
    registered_at: str
    records: list[BatchRecordOut] = Field(default_factory=list)


class BatchSummaryOut(BaseModel):
    batch_id: str
    schema_version: str
    record_count: int
    registered_at: str


class HealthOut(BaseModel):
    status: str
    version: str
