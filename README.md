# 型材定尺裁切排料后端

FastAPI + OR-Tools CP-SAT 的整数优化排料服务。无前端。

## 运行

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8154
```

环境变量 `MAX_CONCURRENT_SOLVES`（默认 2）限制并发求解数。

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

## 测试与示例

```bash
.venv/bin/python -m pytest tests -q
curl -s -X POST http://127.0.0.1:8154/optimize \
  -H 'Content-Type: application/json' \
  -d `examples/sample_request.json | python3 -m json.tool
```

## 代码结构

- `app/models.py` — 请求/响应模型与全部输入校验
- `app/solver.py` — CP-SAT 建模与分阶段字典序优化
- `app/accounting.py` — 排料结果核算与长度守恒校验
- `app/main.py` — HTTP 层、并发限制、线程池求解

## 批次抽样验收

在排料求解之上提供按批次的抽样验收（超几何分布，精确组合数判定，无二项近似、无浮点放宽）。
长度单位：请求中成品标称长度为整数毫米，冻结时转为整数微米；公差偏差与测量值均为整数微米，边界值判定为合格（含边界）。

### POST /batches
请求体（示例见 `examples/batch_request.json`）：

| 字段 | 说明 |
|---|---|
| `batch_id` | 批次 ID（字符串或整数，拒绝布尔） |
| `optimize` | 完整的 `/optimize` 请求体，复用同一求解与核算 |
| `tolerances` | 按 `str(需求id)` 为键的 `{lower_dev_um, upper_dev_um}`，每个需求必须恰好一条；`lower > upper` 视为公差倒置拒绝 |
| `dg` / `db` | 总体可接受/不可接受缺陷数，须满足 `0 <= Dg < Db <= N`（N 为成品总数） |
| `alpha` / `beta` | 生产方/使用方风险，(0,1) 区间内的十进制字符串 |

行为：

- 仅当求解得到完整解（`OPTIMAL`/`FEASIBLE`）才建批；否则返回 `batch_created: false`，不落库。
- 建批时冻结：排料方案、全部成品实例及来源、各需求公差，并在 `1<=n<=N`、`0<=c<n` 内求最小 n 再最小 c，
  满足 `P(拒收|Dg) <= alpha` 且 `P(允收|Db) <= beta`（超几何分布，Fraction 精确比较）。
  响应含 `sampling.sample_size/acceptance` 及实际风险（十进制字符串与精确分数）。
- 均匀无放回抽取 n 个实例并固定，随批次持久化。
- 幂等：同批次同内容重发返回 200 与原方案、原样本；同批次异内容返回 409。并发建批只生效一次，失败不留半份状态（单事务写入）。

### POST /batches/{batch_id}/measurements
请求体 `{demand_id, instance, measured_um}`。仅接受样本内实例（否则 422）；
同值重送幂等返回当前状态，异值不覆盖返回 409。样本未齐不下结论；齐全后缺陷数 `<= c` 则 `ACCEPT`，否则 `REJECT`，结论不可变。

### GET /batches/{batch_id}
查询批次：冻结方案、抽样参数、全部样本及测量值、结论。批次不存在返回 404。

### 存储

SQLite（环境变量 `BATCH_DB_PATH`，默认 `./batches.db`）保存批次、样本、测量与结论，重启后续检。

### 其他校验变更

- `/optimize` 与 `/batches` 现在拒绝 `new_stock` 与 `remnants` 之间跨列表重复 ID（422）。

### 示例

~~~bash
BATCH_DB_PATH=/tmp/batches.db .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8154
curl -s -X POST http://127.0.0.1:8154/batches -H 'Content-Type: application/json' -d `examples/batch_request.json
curl -s -X POST http://127.0.0.1:8154/batches/BATCH-2026-001/measurements -H 'Content-Type: application/json' -d '{"demand_id":"D1","instance":2,"measured_um":1201500}'
curl -s http://127.0.0.1:8154/batches/BATCH-2026-001
~~~
