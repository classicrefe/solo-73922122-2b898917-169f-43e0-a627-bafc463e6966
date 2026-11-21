# Data Quality Gate API（数据质量闸门）

基于 **Python + FastAPI + SQLAlchemy + SQLite** 的数据质量闸门服务，使用 **Docker Compose** 一键启动。
用户提交带 `schema_version` 的批次数据与质量规则，服务执行字段约束、跨记录唯一性与批次级阈值校验，
生成带失败原因的校验报告；**仅当批次通过时**才登记为可供下游读取的批次版本。
对因赠品零金额（`amount_cents=0`）触发 `min` 下界失败而被整批拒收的场景，另提供
**质控复核接口** `POST /batches/review`：在严格核对原始记录与豁免资格后，按剩余错误重算并以新批号登记。

## 一、快速开始

```bash
docker compose up --build -d
# 服务地址 http://localhost:8000，交互式文档 http://localhost:8000/docs
curl http://localhost:8000/health
```

数据持久化在 Docker 卷 `dqg-data`（容器内 `/data/dqg.db`），容器重启后已登记批次与幂等记录不丢失。

本地无 Docker 时可直接运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 二、接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/batches/submit` | 提交批次进行质量校验（幂等） |
| POST | `/batches/review` | 质控复核：豁免零金额 `min` 失败后以新批号重新校验（幂等） |
| GET | `/batches` | 列出所有**已登记**（通过）的批次版本 |
| GET | `/batches/{batch_id}` | 下游读取已登记批次明细；失败/未登记返回 404 |
| GET | `/submissions/{batch_id}` | 查询提交/复核状态与完整校验报告（含失败原因） |
| GET | `/submissions/{batch_id}/report` | 仅查询校验报告 |
| GET | `/submissions/{batch_id}/rules` | 查询该提交实际使用的规则快照 |
| GET | `/catalog/rules` | 查询支持的规则类型目录与参数说明 |
| GET | `/catalog/samples` | 查询示例提交载荷（通过 / 失败各一个）与复核演示样例 |

## 三、规则模型

规则随每次提交内联传入，分三类：

1. **字段约束 `field_rules`**：按字段配置，支持
   `required`、`type`（string/integer/number/boolean）、`min`、`max`、
   `min_length`、`max_length`、`in`（枚举）、`regex`（正则）。
2. **跨记录唯一性 `unique_rules`**：按一个或多个字段的组合在整批内去重；
   任一组成字段为 `null` 的记录跳过唯一性判定。
3. **批次级阈值 `batch_rules`**：
   - `max_failure_rate`（必填，0~1）：失败记录占比上限，超过即整批失败；
   - `min_record_count` / `max_record_count`：批次记录数上下限。

任一规则不满足则批次状态为 `FAILED`，报告中同时返回全部字段失败、唯一性冲突与批次阈值失败及中文原因。

## 四、关键语义

- **幂等**：以 `batch_id` 为幂等键。重复提交相同请求体返回与首次完全一致的报告（`replayed=true`），
  不会重复登记；相同 `batch_id` 但请求体（schema/records/rules）不一致返回 `409`。
- **失败不留可读批次**：`FAILED` 批次只保留提交元数据与报告供排查，不写入 `batches`/`batch_records`，
  下游读取返回 404；`PASSED` 批次才在同一事务内登记批次与全部明细。
- **schema 版本**：每个批次显式携带 `schema_version`，登记与查询均按版本记录。

## 五、质控复核（零金额豁免）

赠品金额 `amount_cents=0`（整数）可能因 `min` 下界规则失败，进而抬高失败率导致整批被拒。
质控员可调用 `POST /batches/review`，传入**原失败批号、原始记录、新批号和已批准豁免的原行号**，
对这批数据做带豁免的重新校验。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `source_batch_id` | 原失败批号，必须是状态为 `FAILED` 的普通提交 |
| `batch_id` | 复核后的新批号（与原批号不同，占用独立幂等键） |
| `schema_version` / `records` / `rules` | 原始记录与规则，必须与来源批次首次提交**逐字节等价** |
| `exempted_record_indexes` | 已批准豁免的原行号列表（自动去重，行号从 0 开始） |

复核规则：

1. **原始记录核对**：请求中的 `records`/`schema_version`/`rules` 与来源批次首次提交不一致时，
   返回 `422` 拒绝复核，且不写入任何数据。
2. **豁免资格**：每个豁免行必须在原报告中确实存在 `amount_cents` 取值为**整数 0**
   （浮点 `0.0`、布尔值均不算）触发的 `min` 下界失败，否则返回 `422`；行号越界同样拒绝。
3. **按剩余错误重算**：仅剔除豁免行的该条 `min` 失败；同一行上的其他字段失败、其他行的失败、
   跨记录唯一性冲突全部保留，并据此重新计算失败行、失败率与批次阈值。
4. **新批号报告**：无论复核结果是 `PASSED` 还是 `FAILED`，都以新批号保存可查询的报告，
   报告的 `review` 字段注明来源批号与豁免行集合。
5. **登记条件**：**只有复核通过**才把整批原始记录（含赠品行）登记为下游可读批次；
   原拒收报告仍可通过 `/submissions/{source_batch_id}` 查询，原批次仍然不可读（`GET /batches` 404）。
6. **幂等与冲突**：
   - 同一新批号、相同来源与豁免行集合（请求体等价）可重复调用，返回首次结果（`replayed=true`）；
   - 普通提交与复核撞号，或同批号复核内容（来源/豁免行/载荷）不同，返回 `409`；
   - 来源批号不存在或不是普通提交返回 `404`。

`/catalog/samples` 的 `review_examples` 提供了完整的四步演示载荷，可直接在 `/docs` 中操作。

## 六、请求示例

```bash
curl -X POST http://localhost:8000/batches/submit \
  -H "Content-Type: application/json" \
  -d "$(curl -s http://localhost:8000/catalog/samples \
      | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["examples"][0]["payload"]))')"
```

复核演示（先拒收，再复核豁免，最后验证可读性）：

```bash
# 1) 提交赠品批次（含两条 amount_cents=0），预期 FAILED 且不可读
curl -s -X POST http://localhost:8000/batches/submit \
  -H "Content-Type: application/json" \
  -d "$(curl -s http://localhost:8000/catalog/samples \
      | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["review_examples"][0]["steps"][0]["payload"]))')"

# 2) 复核：豁免行 1、3 的零金额 min 失败，预期 PASSED 并以新批号登记
curl -s -X POST http://localhost:8000/batches/review \
  -H "Content-Type: application/json" \
  -d "$(curl -s http://localhost:8000/catalog/samples \
      | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["review_examples"][0]["steps"][1]["payload"]))')"
```

## 七、目录结构

```
app/
  main.py        # FastAPI 入口（lifespan 建表 + 旧库补列迁移）
  config.py      # 环境变量配置
  database.py    # SQLAlchemy 引擎/会话/SQLite PRAGMA/轻量迁移
  models.py      # ORM：submissions（含 kind/来源/豁免列）/ batches / batch_records
  schemas.py     # Pydantic 请求、复核与报告模型
  validation.py  # 纯函数校验引擎（三类规则 + 豁免感知复核）
  services.py    # 幂等提交、质控复核、事务登记、查询
  routers.py     # HTTP 路由
data/
  rule_catalog.json     # 规则目录
  sample_payloads.json  # 示例数据与复核演示样例
tests/
  test_api.py                 # 接口自测（pytest + TestClient）
  test_review.py              # 复核 API 全路径自测
  test_concurrency.py         # 普通提交并发竞态
  test_review_concurrency.py  # 复核并发竞态
```

## 八、自测

```bash
pip install -r requirements.txt pytest httpx
pytest tests/ -v
# 脚本式并发用例（pytest 之外的直接断言脚本）：
PYTHONPATH=. python3 tests/test_concurrency.py
PYTHONPATH=. python3 tests/test_review_concurrency.py
```

升级说明：新版本会在启动时对旧版 `submissions` 表自动补齐复核所需列
（`kind`、`payload_json`、`source_batch_id`、`exempted_indexes_json`），
持久化 Docker 卷中的历史数据无需手工迁移。
