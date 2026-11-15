# A/B 实验分流校验与显著性判定（`abcheck`）

> 数据工程 · 数据应用 · 中型多模块系统。原生 HTML/CSS/JS 前端 + Python FastAPI 后端 + Celery worker + MySQL 8.0 + Redis，5 个容器一键起。

## 1. 场景

- **模拟客户**：橙野 App · 增长实验组（7 人），每周 8 个实验，1 个 App × 4 个渠道（信息流 / 推送 / 搜索 / 小程序）。
- **现有工作流**：实验平台只负责分流，结果导出 CSV 丢给分析师跑 SQL + Excel，
  卡方和 t 检验各算一遍，p 值 < 0.05 就写进上线邮件。
- **每日回传的双源现实**：实验平台（分流数）与指标库（转化数）每天各回传一次。
  批次重试或补数后两边总量经常对不上，页面若直接采用「最近一次上传」，就会出现
  **分流校验用一边的数据、显著性沿用另一边的旧结果**，负责人据此做上线决定。
- **三个痛点**：
  1. **分流不均没查（SRM）**：某 SDK 版本把 50/50 分成了 52.3/47.7，对照组凭空多 138 个样本，指标差异全被污染；
  2. **只看 p 值不看功效与最小可检测效应**：5,000 份样本去检测 1.1pp 的效应，功效只有 45%，算出来的「显著」其实是噪音；
  3. **多指标同时看没做多重比较校正**：一次实验 4 个指标，随便哪个 p<0.05 就宣布成功，假发现率失控。
- **上下游**：埋点 → 实验平台（分流）→ 指标库（转化）→ **本系统（逐日对账 → 分流校验 → 指标检验 → 决策建议）** → 产品决策会 / 上线评审。
- **一天的工作剧本**：
  1. 09:20 实验负责人 `admin@abcheck.io` 登录，总览上 `EXP-2603-CARTBTN` 挂着 **SRM 告警**（χ²=12.696, p=0.000366）；
  2. 09:40 打开「回传对账」，看到 7 天里实验平台 7 批 / 指标库 7 批；某实验最后一天指标库批次没到 → **延迟**，逐格标红，分析批次被门禁挡住；
  3. 10:00 另一实验有 1 格转化数差 −3 → **冲突**，填「指标库去重了重复上报的订单」并采用指标库 → 转 **已和解**；
  4. 10:20 冲突格一变，引用旧数据的分析批次立刻 **作废**，负责人的决策同步转 **过期**；重新生成时只重算受影响那一天，其余 6 天沿用；
  5. 11:00 打开 `EXP-2610-CASH8`，主指标下单转化率 10.09% → 11.19%，lift +10.89%，Wald CI [+0.25pp, +1.94pp]；
  6. 11:10 切到「指标检验」跑 BH 校正：4 个指标里主指标 q=0.0436 < 0.05，另外 3 个不显著；森林图上只有主指标的点越过 0 之外；
  7. 11:30 出决策「上线」，理由自动带上 lift / q / 样本量 / 分析批次号，写进 06-22 决策会纪要；
  8. 14:00 分析师 `analyst@abcheck.io` 导出 `metrics.csv` 与 `analysis-runs.csv`（28 条检验结果 + 双方批次数）做周报，审计日志记下这次导出；
  9. 17:30 业务只读 `viewer@abcheck.io` 打开总览和实验台账，导出按钮不存在；直接请求导出接口真实返回 **403**。

## 1.1 逐日双源对账口径（核心新增）

| 概念 | 口径 |
|---|---|
| 回传批次 | 自然键 `(实验, 来源, 统计日, 批次号)`；`revision` 从 1 起递增 |
| 同批次重试 | 同批次号 + **同内容指纹** → `duplicate`：不新增批次、不重复累计、不触发重算 |
| 改数 | 同批次号但内容指纹变化 → `revision + 1`，旧批次 `superseded`，受影响统计日标脏 |
| 新的批次号 | 同来源同一天再报一次 → 旧活动批次被取代，新批次生效 |
| 逐格对账键 | `(变体, 渠道, 指标)`，外层按统计日 |
| 齐全 `complete` | 两侧都有且分流数、转化数完全一致 |
| 延迟 `delayed` | 只有一侧有这一格；**不允许单边凑合**，必须等另一侧回传 |
| 冲突 `conflict` | 两侧都有但数值不同（含转化数超过分流数） |
| 已和解 `reconciled` | 冲突格填写解释 + 指定采用侧（`assignment` / `metric`）后才可用 |
| 分析门禁 | 参与分析的**同一批统计日**必须全部 `complete`/`reconciled`，否则 422 `NOT_RECONCILED` |
| 分析批次 | 在一份 canonical 快照上同时算分流校验 + 指标结果 + 决策依据，并记录双方原始批次数 |
| 作废 | 源数据变化 / 新解释 → 引用旧快照的分析批次 `invalidated`，其决策转 `stale` |
| 增量重算 | 按统计日指纹比对，只有变化日计入 `recomputed_dates`，其余进 `reused_dates`；指纹不变直接复用 |
| 溯源导出 | 所有 CSV 带双方批次数、revision、差异原因、采用侧、分析批次号与使用统计日 |

## 2. 角色与权限

| 角色 | 账号 | 权限 | 典型动作 |
|---|---|---|---|
| 实验负责人 `admin` | `admin@abcheck.io` | 读写实验、录分流样本、跑 SRM/指标检验、出决策、导出、看审计 | 建实验、跑 χ²、写决策 |
| 数据分析师 `analyst` | `analyst@abcheck.io` | 读实验/样本/检验/决策 + 导出 CSV | 导出 `metrics.csv` 做周报 |
| 业务只读 `viewer` | `viewer@abcheck.io` | 只读列表与详情 | 看总览、翻实验台账 |

密码统一 `Demo@2026`。跨权调用返回真实 HTTP 状态：无 token **401**，viewer 写/导出/审计 **403**，
非法参数 **422**，重复自然键（实验编号 / 分流样本格 / 同一实验的第二条决策）**409**。

## 3. 业务模块表（6 个）

| # | 业务模块 | 对应页面 | 后端 services / controller | 主要 API |
|---|---|---|---|---|
| ① | 实验登记（实验 + 变体） | `#/experiments` 实验登记 | `experiment_service` / `variant_service` · `experiment_controller` | `GET/POST /api/experiments`、`GET/PATCH /api/experiments/{id}` |
| ② | 每日双源回传 + 逐格对账 | `#/reconciliation` 回传对账 | `return_service` / `reconciliation_service` · `reconciliation_controller` | `GET/POST /api/experiments/{id}/return-batches`、`GET/POST /api/experiments/{id}/reconciliation`、`GET .../reconciliation/cells`、`POST .../cells/{cid}/explain` |
| ③ | 分流样本接入（变体×渠道×日期×指标） | `#/assignments` 样本与分流 | `assignment_service` / `canonical_service` · `assignment_controller` | `GET /api/experiments/{id}/samples`、`GET/POST /api/experiments/{id}/assignments` |
| ④ | 分流校验 + 指标检验（算法 A/B，成套产出） | `#/metrics` 指标检验 + `#/overview` 告警 | `analysis_service` + `algorithms/{srm,metric_test,reconcile}.py` · `metric_controller`、`reconciliation_controller` | `GET /api/experiments/{id}/srm`、`POST /api/experiments/{id}/srm-check`（仅手工样本实验）、`POST /api/experiments/{id}/analysis-runs`、`GET .../analysis-runs/latest` |
| ⑤ | 多重比较与决策（引用有效批次、改判留史） | `#/metrics`（q 值对比）+ `#/decisions` | `metric_service` / `decision_service` · `decision_controller` | `GET /api/decisions`、`POST /api/experiments/{id}/decisions`、`GET .../decision-revisions` |
| ⑥ | 报告导出与台账总览 | `#/decisions`（导出）+ `#/overview` | `export_service` / `overview_service` · `export_controller` | `GET /api/exports/{experiments,assignments,metrics,decisions,reconciliation,analysis-runs}.csv`、`GET /api/overview` |

> **成套口径**：实验一旦有每日回传，分流校验与指标检验不能在 `srm-check` / `metric-tests`
> 单独跑（409 CONFLICT），必须通过 `POST /analysis-runs` 在同一份 canonical 快照上生成
> 「分流校验 + 指标结果 + 决策依据」，避免页面两侧各用一份旧数据。手工录入样本的存量实验
> （无回传批次）保留旧的单独校验入口。

横切能力（不算业务模块）：登录/JWT、角色鉴权、审计日志、限流、worker 心跳。

## 4. 快速启动

```bash
cp .env.example .env
docker compose up -d --build   # 等 5/5 healthy（实测约 40s）
open http://localhost:18207
```

首次启动 backend 自动建表并跑一次 seed（`SEED_DEMO=true`）。seed 幂等，可重复执行：

```bash
docker compose exec -T backend python -m scripts.seed_demo
# SEED_SUMMARY brand=abcheck users=3 sources=6 runs=1092 findings=19 pending=2 batches=85
```

## 5. 本地开发

```bash
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --reload --port 8000   # /api 前缀
cd frontend/site && python -m http.server 5173
```

## 6. 访问地址

| 服务 | 地址 |
|---|---|
| 前端 | http://localhost:18207 |
| 后端 API 文档 | http://localhost:19207/docs |
| 健康检查 | http://localhost:19207/api/health |
| 数据库映射 | localhost:17707 |

## 7. 技术栈

| 层 | 选型 |
|---|---|
| 前端 | 原生 HTML5 + CSS（oklch 令牌 / 网格纸底纹）+ ES Modules；无框架、无打包器、除 Google Fonts 外无 CDN |
| 后端 | Python 3.11、FastAPI 0.115、SQLAlchemy 2.0、PyJWT、passlib(bcrypt) |
| 算法 | 纯 `math` 手写：卡方上尾（不完全伽马）、正态分位数、双比例 z 检验、Bonferroni / BH-FDR |
| 异步 | Celery 5.4 + Redis（10s 心跳、07:40 每日 SRM 复算） |
| 数据库 | MySQL 8.0（utf8mb4） |
| 部署 | Docker Compose，恰好 5 服务（frontend / backend / worker / db / redis） |

## 8. 目录

```text
backend/
  src/{config,constants,controllers,middlewares,models,routes,schemas,services,utils}
  src/services/algorithms/{stats,srm,metric_test,reconcile}.py   # 两个统计算法 + 回传对账纯函数
  scripts/{seed_demo.py,demo_config.py,worker_healthcheck.sh}
  migrations/  tests/
frontend/site/{index.html,css/{tokens,app}.css,js/{config,api,store,ui,app}.js,js/pages/*.js}
```

## 9. 环境变量

见 `.env.example`：`COMPOSE_PROJECT_NAME`、`FRONTEND_PORT=18207`、`BACKEND_PORT=19207`、`DB_PORT=17707`、
`DB_*`、`JWT_SECRET`、`REDIS_URL`、`SEED_DEMO`、`RATE_LIMIT_*`、`APP_TIMEZONE_OFFSET_MINUTES`。

## 10. 核心算法与口径

### 算法 A｜SRM 与功效（`src/services/algorithms/srm.py`）

- 输入：实验的变体列表（计划比例 `ratio`）+ 各变体主指标分流样本合计 `observed` + 实验登记的基准转化率 `baseline_rate`、目标最小效应 `planned_mde`。
- 卡方：`χ² = Σ (obs_i - exp_i)² / exp_i`，`exp_i = N · ratio_i`，`df = k - 1`；
  p 值 = 卡方上尾概率 = 正则化上不完全伽马 `Q(df/2, χ²/2)`（级数 + Lentz 连分式，纯 math 实现）。
- 判定：`p < 0.001` → `srm`（SRM 告警），否则 `balanced`。
- 样本量：`n = 2(z_{1-α/2} + z_{1-β})² · p(1-p) / Δ²`，逐变体输出 `required` 与 `gap = max(0, n_required - n_observed)`。
- 功效与 MDE：`power = Φ(√(nΔ²/(2p(1-p))) - z_{1-α/2})`；`MDE = (z_{1-α/2}+z_{1-β})·√(2p(1-p)/n)`。
- 失败态：变体 < 2、样本合计为 0、计划比例合计为 0 → 422（`NO_SAMPLES` / `VALIDATION`）。

### 算法 B｜双比例检验 + 多重校正（`src/services/algorithms/metric_test.py`）

- 输入：同一实验同一变体下每个指标的 对照/实验 样本量与转化数（来自 `assignments` 聚合）。
- 检验：`p̄ = (c₁+c₂)/(n₁+n₂)`，`z = (p₁-p₂)/√(p̄(1-p̄)(1/n₁+1/n₂))`，双侧 `p = 2(1-Φ(|z|))`。
- 区间：Wald 95% CI `Δ ± z_{1-α/2}·√(p₁(1-p₁)/n₁ + p₂(1-p₂)/n₂)`；lift CI 由同一区间除以 p₂。
- 校正：Bonferroni `q = min(1, m·p)`；BH-FDR 升序 `q_(i) = min_{k≥i}(m·p_(k)/k)` 并单调化；两者都落库。
- 结论：`q < 0.05` 且 Δ>0 → `ship`（上线）；`q < 0.05` 且 Δ<0 → `stop`（停止）；否则 `continue`（继续观察）。
- 失败态：样本量为 0、转化数越界、method 非 `bh|bonferroni` → 422。

两个算法都是纯函数（除落库层），同样输入两次逐位一致；`backend/tests/` 覆盖正常 / 边界 / 确定性。

### 算法 C｜逐日双源对账（`src/services/algorithms/reconcile.py`）

- 输入：实验平台侧与指标库侧同一天的 `(变体, 渠道, 指标, 分流数, 转化数)` 行。
- 逐格分类：两侧齐全且数值一致 → `complete`；只有一侧 → `delayed`（不可用，必须等另一侧）；
  两侧数值不同 → `conflict`；冲突格填解释 + 指定采用侧 → `reconciled`。
- 当天状态：任一格延迟 → `delayed`；否则任一格未解释冲突 → `conflict`；否则有和解 → `reconciled`；
  全部齐全 → `complete`。只有 `complete` / `reconciled` 才能进分析。
- 批次指纹：规范化行排序后 SHA-256，用于「同批次号重试」与「改数」的区分。
- 快照聚合与指纹：按统计日聚合 canonical 值，逐日指纹用于增量重算 —— 只重算变化日。
- 全程纯函数（不碰数据库），定义见 `tests/test_reconcile.py`（11 个用例）。

全部单测：`backend/tests/` 共 40 个用例（统计 18 + 对账 11 + 服务层 11）。

## 11. 共享枚举出现位置

| 枚举 | 后端 | 前端 |
|---|---|---|
| 角色 `admin/analyst/viewer` | `backend/src/constants/{roles,enums}.py` | `frontend/site/js/config.js`（`ROLE_LABELS` / `PERMISSIONS`） |
| 实验状态 `draft/running/paused/finished/archived` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.experimentStatus`） |
| 决策结论 `ship/continue/stop` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.verdict`） |
| SRM 状态 `balanced/srm/unknown` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.srm`） |
| 渠道 `feed/push/search/mp` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.channel`） |
| 指标口径 `cart_rate/order_rate/retention_d1/click_rate/search_success` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.metric`） |
| 校正方法 `bh/bonferroni` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.correction`） |
| 回传来源 `assignment/metric` | `backend/src/constants/enums.py`（`ReturnSource`） | `frontend/site/js/config.js`（`ENUMS.reconcileSource` / `reconcileSourceShort`） |
| 对账状态 `complete/delayed/conflict/reconciled` | `backend/src/constants/enums.py`（`ReconcileStatus`） | `frontend/site/js/config.js`（`ENUMS.reconcile` + `reconcileTone`） |
| 差异原因 `missing_*/samples_differ/conversions_differ/...` | `backend/src/services/algorithms/reconcile.py`（`REASON_LABELS`） | `frontend/site/js/config.js`（`ENUMS.reconcileReason`） |
| 分析批次状态 `ready/invalidated/recomputing` | `backend/src/constants/enums.py`（`AnalysisStatus`） | `frontend/site/js/config.js`（`ENUMS.analysis` + `analysisTone`） |
| 决策状态 `active/stale` | `backend/src/models/decision.py` | `frontend/site/js/pages/decisions.js` |

## 12. Docker 说明

- 5 个服务：`frontend`(Nginx 静态站) / `backend`(uvicorn) / `worker`(Celery) / `db`(MySQL 8.0) / `redis`，全部带 healthcheck。
- 命名卷：`db_data`、`redis_data`；`depends_on: service_healthy`；端口 `${FRONTEND_PORT:-18207}` 写法。
- Nginx：`location /api/ { proxy_pass http://backend:8000/; }` + SPA 回落。
- 收尾：`docker compose down -v --remove-orphans`。

## 13. 视觉方向

实验室记录本：石墨灰墨色（`--ink oklch(0.285 0.012 250)`）+ 芥末黄（`--accent oklch(0.745 0.135 85)`）+
网格纸底纹（body 双层 grid 渐变）、手写批注（Caveat，`.note`）、印章（`.stamp`）；
登录页做成「实验登记页」（Lab Sheet A-2607，表头 + 受控章 + 手写提示），总览做成台账 + 告警页。

## License

内部演示项目，仅用于评测与教学。
