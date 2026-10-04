# 型材定尺裁切排料后端

FastAPI + OR-Tools CP-SAT 的整数优化排料服务。无前端。

## 运行

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8154
```

环境变量 `MAX_CONCURRENT_SOLVES`（默认 2）限制并发求解数。

SQLite 批次库路径由环境变量 `BATCH_DB_PATH` 控制（默认 `batches.db`，WAL 模式）。

## 接口

### GET /health
返回 `{"status": "ok", "solver_slots_available": N}`。求解在线程池执行，不阻塞健康检查。

### POST /optimize
请求体（长度均为整数毫米，价格为整数分）：

| 字段 | 说明 |
|---|---|
| `demands[]` | `id, material, length>0, quantity>=1` |
| `new_stock[]` | `id, material, length>0, price>=0, available>=1` |
| `remnants[]` | `id, material, length>0`（现有余料，不计采购价） |
| `kerf` | 锯缝宽度，>=0 |
| `tail_threshold` | 尾料保留阈值，>=0；达到阈值的尾料可复用、不计损耗 |
| `budget_ms` | 毫秒预算，覆盖全部优化阶段 |

校验（违反返回 422）：拒绝布尔数、负值、非整数、零/负数量、重复 ID；
展开后成品总数 <= 60 件、原料（新料实例 + 余料）总数 <= 24 根。
同一 ID 在新料与余料两个列表间重复同样拒绝。

响应：

- `status`: `OPTIMAL`（三阶段均证明最优）/ `FEASIBLE`（预算耗尽，有完整可行解但未证明最优）/ `INFEASIBLE`（已证明无解）/ `NO_SOLUTION_WITHIN_BUDGET`（预算耗尽且未找到任何完整解）。
- `layouts[]`: 每根的来源（`new_stock`/`remnant` + 源 ID + 实例号）、切割顺序（成品 ID + 实例号）、`kerf_total`、`tail`、`tail_reusable`。无完整解时不返回任何部分排料。
- `metrics`: `total_cost`（新料采购总价）、`total_loss`（锯缝 + 低于阈值的尾料）、`bars_used`。
- 长度守恒：每根 `sum(cuts) + kerf_total + tail == bar_length`，服务端在返回前校验。

## 优化语义

- 成品不可拼接，仅使用同材质原料；同一余料不会被重复利用。
- 锯缝规则：每取下一件计一次锯缝；最后一件恰好用尽余下长度（tail==0）时该次不计。
- 目标按字典序严格优先：1) 采购总价 2) 不可复用损耗总长 3) 使用根数。
  前一阶段证明最优后才以其最优值为约束进入下一阶段；预算按 50%/30%/20% 切分给三个阶段，
  某阶段耗尽预算未证明最优即停止并返回当前最优完整解（`FEASIBLE`）。

## 边界

- 单件长度超过所有可用原料 → `INFEASIBLE`。
- `kerf=0`、`tail_threshold=0`（任何尾料均可复用）均合法。
- 容量占满时新请求立即返回 429；失败或完成后都会释放容量。
- 请求间无共享状态，求解相互隔离。

## 批次抽样验收

在原排料接口之上增加按批次的计量型抽样验收，复用同一套求解与核算逻辑。

### POST /batches
请求体：

| 字段 | 说明 |
|---|---|
| `batch_id` | 批次 ID（非空字符串或非布尔整数） |
| `request` | 与 `POST /optimize` 完全相同的原排料请求 |
| `tolerances` | 每个需求 ID 一个 `{lower, upper}` 上下偏差（整数微米），下界不得大于上界；每个需求必须有公差，未知需求的公差被拒绝 |
| `dg` / `db` | 可接受/不可接受缺陷数，要求 `0 <= dg < db <= N`（N 为成品总件数）；布尔数拒绝 |
| `alpha` / `beta` | 生产方/使用方风险，`[0,1]` 内的十进制**字符串**（如 `"0.05"`），数字与布尔拒绝 |

行为：

- 先复用求解器与核算；只有得到完整解（`OPTIMAL`/`FEASIBLE`）才建批，无解/超时返回 422 且不落任何状态。
- 冻结内容：排料方案（含每根来源与切割顺序）、全部成品实例及来源、各需求公差。
- 标称长度按 mm×1000 转为整数微米；偏差与测量同单位（微米），边界值 `nominal+lower <= m <= nominal+upper` 判合格。
- 抽样方案：在 `1<=n<=N`、`0<=c<n` 内按**超几何分布**精确搜索，先最小 `n` 再最小 `c`，
  使 Dg 批拒收概率 `<= alpha`、Db 批允收概率 `<= beta`。组合数用 `math.comb` 精确整数计算，
  风险用 `Decimal` 比较，不使用二项近似或浮点放宽；返回 `n`、`c` 与实际双方风险（精确小数字符串）。
- 样本用 `SHA256(batch_id|内容哈希)` 作种子的均匀无放回抽样，建批即固定。
- 同批次同内容（排料请求、公差、dg/db/alpha/beta）重放返回原方案、原样本，`created:false`；
  异内容返回 409。整批写入在单个 `BEGIN IMMEDIATE` 事务内完成，并发建批只生效一次，失败不留半份状态。

### PUT /batches/{batch_id}/pieces/{demand_id}/{instance}

逐件提交测量：`{"measured_length": <整数微米>}`。

- 只接受固定样本中的实例；非样本实例或未知批次返回 404。
- 同值重送幂等；异值重送返回 409 且不覆盖。
- 未测齐（`< n`）时 `status=PENDING`、不下结论；齐全后缺陷数 `<= c` 为 `ACCEPTED`，否则 `REJECTED`，结论永久不变。

### GET /batches 与 GET /batches/{batch_id}

列出批次，或查询单批：抽样方案与实际风险、固定样本（标称值、偏差、是否已测、是否缺陷）、测量/缺陷计数、结论、冻结方案与指标。重启进程后从 SQLite 恢复，可继续测量（续检）。

## 测试与示例

```bash
.venv/bin/python -m pytest tests -q
curl -s -X POST http://127.0.0.1:8154/optimize \
  -H 'Content-Type: application/json' \
  -d @examples/sample_request.json | python3 -m json.tool

# 建批（含原请求与每需求微米公差、dg/db/alpha/beta）
curl -s -X POST http://127.0.0.1:8154/batches \
  -H 'Content-Type: application/json' \
  -d @examples/sample_batch.json | python3 -m json.tool

# 逐件提交样本实例测量（整数微米，边界合格）
curl -s -X PUT http://127.0.0.1:8154/batches/BATCH-DEMO-1/pieces/D1/1 \
  -H 'Content-Type: application/json' \
  -d '{"measured_length": 1199900}' | python3 -m json.tool

curl -s http://127.0.0.1:8154/batches/BATCH-DEMO-1 | python3 -m json.tool
```

## 代码结构

- `app/models.py` — 请求/响应模型与全部输入校验
- `app/solver.py` — CP-SAT 建模与分阶段字典序优化
- `app/accounting.py` — 排料结果核算与长度守恒校验
- `app/main.py` — HTTP 层、并发限制、线程池求解
- `app/sampling.py` — 精确超几何抽样方案搜索（组合数 + Decimal 风险）
- `app/storage.py` — SQLite 批次/样本/测量/结论持久化、固定抽样、事务与幂等
