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
  -d @examples/sample_request.json | python3 -m json.tool
```

## 代码结构

- `app/models.py` — 请求/响应模型与全部输入校验
- `app/solver.py` — CP-SAT 建模与分阶段字典序优化
- `app/accounting.py` — 排料结果核算与长度守恒校验
- `app/main.py` — HTTP 层、并发限制、线程池求解
