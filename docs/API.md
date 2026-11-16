# 接口 anomalyattr

服务端口 `18209`，容器内 `8000`。所有金额与因子以字符串返回，避免 JSON 浮点误差。

## 页面

| 路径 | 内容 |
|---|---|
| `/` | 指标接入、四账、原因码、批次刻度 |
| `/conflicts` | 冲突组裁定：原证据与当前决定对照、采信、撤销 |
| `/anomalies` | 水位时间轴、确认点与候选点、事件清单 |
| `/review` | 复盘卡、敏感性矩阵、同批异常、逐日证据 |
| `/attribution` | 三因子分解、门店贡献、窗口覆盖 |
| `/drilldown` | 渠道分解与门店明细 |
| `/batches` | 批次刻度、运行清单、同源重跑 |
| `/report` | 运行报告与导出口 |

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活与库大小 |
| GET | `/api/import/samples` | 样例清单（名称、字节、sha256） |
| POST | `/api/import/preview` | 只采信：编码、列映射、诊断前 20 条 |
| POST | `/api/import` | 整批导入：采信 → 归一 → 质量 → 落库 → 清单 |
| GET | `/api/contract` | 字段定义、派生字段、规则、阈值、节假日 |
| POST | `/api/contract/preview` | 单行试算，返回归一后字段或原因码 |
| GET | `/api/quarantine` | 隔离明细、原因分布、三账 |
| GET | `/api/quarantine.csv` | 隔离行 CSV（行号 + 原因码 + 原值） |
| GET | `/api/conflicts` | 冲突组清单：原快照、当前决定、裁定采信收入拆分 |
| GET | `/api/conflicts/{group_id}` | 单组证据链：候选快照 + 历次裁决 + 当前有效点 |
| POST | `/api/conflicts/{group_id}/adopt` | 采纳指定快照，需 `line_no`、`reason`、`actor`、`expected_version` |
| POST | `/api/conflicts/{group_id}/revoke` | 撤销最近采信，恢复整组隔离，同样需版本校验 |
| GET | `/api/anomalies` | 检测结果；`status` / `channel` / `hotel` 可筛选 |
| GET | `/api/anomalies/timeline` | 日期刻度 + 异常标记 + 水位 |
| GET | `/api/attribution` | `dimension=hotel_code\|channel`、`cur_days`、`base_days` |
| POST | `/api/attribution/rerun` | 同源重跑，命中同一批次时 `reused=true` |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/batches/{run_id}` | 运行清单、参数摘要、账目备注 |
| GET | `/api/reconcile` | 四账与逐条检查 |
| GET | `/api/report` | 报告结构与结论 |
| GET | `/api/export/{kind}` | `points.csv` / `anomalies.csv` / `attribution.csv` / `manifest.json` |
| GET | `/api/review` | 复盘卡、敏感性九组合、同批异常；`run_id` 可指定批次 |
| GET | `/api/review/{event_id}` | 单张复盘卡（含逐日证据与同向对照） |
| GET | `/api/review/sensitivity` | 单组合明细：`window` 14-42、`threshold` 1-10 |
| GET | `/api/review/cards.csv` | 复盘卡 CSV（BOM + 标记码） |

## 失败返回

| 场景 | 状态码 | `error` |
|---|---|---|
| 样例不存在 | 404 | `E_SAMPLE_NOT_FOUND` |
| 空文件 | 400 | `E_EMPTY_FILE` |
| 缺必需列 | 400 | `E_MISSING_COLUMNS` |
| 末行截断 | 400 | `E_TRUNCATED` |
| 仅表头 / 全空行 / 0 记录 | 422 | `E_HEADER_ONLY` / `E_ALL_BLANK` / `E_NO_ROWS` |
| 维度非法 / 窗口越界 | 400 | `E_BAD_DIMENSION` / `E_BAD_WINDOW` |
| 批次不存在 | 404 | `E_BATCH_NOT_FOUND` |
| 还没有批次 | 404 | `E_NO_RUN` |
| 导出类型未知 | 400 | `E_BAD_EXPORT` |
| 复盘窗口或阈值越界 | 400 | `E_BAD_AXIS` |
| 复盘卡不存在 | 404 | `E_CARD_NOT_FOUND` |
| 当前批次没有有效点 | 400 | `E_NO_POINTS` |
| 冲突组不存在 | 404 | `E_CONFLICT_GROUP_NOT_FOUND` |
| 裁决理由不足 4 字 / 裁决人为空 | 400 | `E_DECISION_REASON_REQUIRED` / `E_DECISION_ACTOR_REQUIRED` |
| 缺少或非法的依据版本 | 400 | `E_DECISION_VERSION_REQUIRED` |
| 所选快照不在该组 | 400 | `E_DECISION_UNKNOWN_LINE` |
| 版本已过期（并发裁决） | 409 | `E_DECISION_STALE` |
| 无可撤销的裁决 / 已处于隔离 | 400 | `E_DECISION_NOTHING_TO_REVOKE` / `E_DECISION_ALREADY_ISOLATED` |

错误体固定为 `{"error": "...", "message": "..."}`，空态额外带 `empty_state`；`E_DECISION_STALE` 额外带 `current_version`。

### 裁决请求体

```json
{"line_no": 493, "reason": "财务核实以该快照为准", "actor": "finance-a",
 "expected_version": 0, "request_id": "可选，用于安全重试"}
```

- `expected_version` 必填，取自当前组的 `version`；服务端在同一事务里校验它是组内下一个版本，否则返回 409。
- `request_id` 可选且按组唯一；同一请求号重放直接返回原决定（`reused=true`），不会二次入账。
- 采纳后该快照进入收入、检测与归因，但原始冲突行与 `Q_DUP_CONFLICT` 原因码保持不变；撤销新增一条 `revoke` 记录，历史全部保留。
