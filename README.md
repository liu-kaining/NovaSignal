# 🚀 NovaSignal (novasignal.thetamind.ai)

> 完全 Serverless 的 AI-Native 港美股打新与财报监控雷达。
> **每天 3–5 篇精品研报 > 每天 20 篇平庸研报。**

NovaSignal 用 **GitHub Actions** 做调度，用 **Python + FMP (Financial Modeling Prep)** 拉取并结构化财务与市场数据，再**异步拉起 Claude Code Agent** 在独立沙盒里走 **Drafter → Reviewer → Reviser** 三阶段流水线产出 Markdown 研报与结构化 `metrics`。每篇报告都经过：

1. 自动 8 项 Web 检索（EDGAR / IR / 财经媒体）
2. 独立 Reviewer agent 评审打分
3. Reviser agent 外科手术式修订
4. Python 端硬性 QA gate 验证（语言标签 / 引用数量 / Unknown 计数 / URL 实时 HEAD 抽检）

## ✨ 核心特性

* **三阶段 Agentic Pipeline（v4）：**
  * **A. Drafter** — 读取 13 个主题数据分片 + INDEX.md，强制完成 8 项 Web 检索，产出 `report.md` / `metrics.json` / `research_notes.md`
  * **B. Reviewer** — 独立 sandbox + 全新上下文，10 维度评审，独立 WebFetch 抽检 URL，产出 `critique.md`
  * **C. Reviser** — 根据 critique 做最小幅度精修，绝不全篇重写
  * **Python QA gate** 每个 stage 后自动验证；不通过自动 feedback 重试 1 次

* **Subagent 拆任务（Claude Code Task 工具）：** Drafter 被引导显式 spawn `edgar_researcher` / `peer_analyst` / `macro_strategist` 三个子 agent 并行深挖，各自享有独立 200k 上下文窗口。

* **数据分片可寻址：** 每个 symbol 的 `raw_data` 切成 13 个主题文件（ipo / profile / financials / ownership / sell_side / peers / news / filings / market / sector / macro / fundraising / fetch_errors），配套 `INDEX.md` 数据目录，agent 按需 Read，不再一次性吞噬几百 KB JSON。

* **Python 硬性 QA gate：** 服务端验证（不依赖 agent 自查）：
  * 禁止 `(English)` / `(中文)` 等语言标签出现
  * 强制 ≥8 个真实 URL 引用（最终 stage）
  * `Not available / Unknown` 类表述 ≤5
  * 8 章节齐全 + `metrics.json` 合法 + 必填字段完整
  * URL HEAD 抽检（防伪造链接）

* **FMP 深度增强：** 每轮流水线先做一次 **全局预取**（IPO 监管披露/招股书列表、宏观利率/经济日历/指标快照、行业板块表现、主要指数报价），再按标的合并进 raw_data（财报、可比公司、卖方、持股治理、SEC、新闻、板块语境等），全部物化到沙箱 `data/*.json`。

* **R2 数据布局（生产模式）：**
  * `reports/{YYYY-MM-DD}/{SYMBOL}_report.md` — 最终研报
  * `metrics/{YYYY-MM-DD}/{SYMBOL}_metrics.json` — 结构化评分
  * `research_notes/{YYYY-MM-DD}/{SYMBOL}_research_notes.md` — **Drafter 的 Web 研究日志（审计页）**
  * `critique/{YYYY-MM-DD}/{SYMBOL}_critique.md` — **Reviewer 的结构化评审**
  * `quality_gate/{YYYY-MM-DD}/{SYMBOL}_gate.json` — Stage outcome 与最终 gate 结果（供 evolution pipeline 使用）
  * `debug/{YYYY-MM-DD}/{SYMBOL}/{stage}/{file}` — **失败诊断**：阶段超时/崩溃时保留的部分沙箱状态（如未完成的 `research_notes.md` / `_progress.log`），用于 post-mortem 分析
  * `raw_data/{YYYY-MM-DD}/{SYMBOL}_raw_data.json` — 输入侧 FMP 快照
  * `raw_data/_shared/{YYYY-MM-DD}/fmp_prefetch_bundle.json` — 全局预取包
  * `state/{YYYY-MM-DD}/{SYMBOL}_state.json` — 状态机日志

* **零服务器 (Serverless)：** 无常驻机；密钥走 GitHub Secrets，对象存储走 Cloudflare R2。

* **静态站点：** `deploy_site` 工作流只从 R2 拉取 **`reports/`** 写入 Hugo；`raw_data/` / `research_notes/` / `critique/` / `quality_gate/` 留在桶内供审计或下游系统使用。

* **幂等与防刷：** 自动发现模式下会按 R2 已有 `reports/` 过滤已分析标的；`--max-symbols` 限制单 run 上限（默认 5），追求精品而非吞吐。

## 📂 目录结构

```text
novasignal/
├── .github/workflows/         # Actions：定时/手动跑流水线、站点部署、evolution
├── src/
│   ├── orchestrator/
│   │   ├── run_pipeline.py        # CLI 入口 + 整体编排
│   │   ├── multi_stage_runner.py  # Drafter → Reviewer → Reviser 三阶段
│   │   ├── data_partitioner.py    # raw_data → 13 文件 + INDEX.md
│   │   ├── quality_gate.py        # 服务端硬性 QA 校验 + URL HEAD 抽检
│   │   ├── sandbox_manager.py     # 多 sandbox 生命周期
│   │   ├── async_runner.py        # Claude Code CLI 子进程包装
│   │   └── github_pages.py        # R2 reports → Hugo content 同步
│   ├── fetchers/                  # FMP 客户端与限流/缓存
│   ├── evolution/                 # 偏差计算与 prompt 自动校正
│   └── storage/                   # R2 客户端
├── prompts/
│   ├── ipo_drafter_template.md    # Stage A 合约
│   ├── ipo_reviewer_template.md   # Stage B 合约
│   └── ipo_reviser_template.md    # Stage C 合约
├── hugo/                          # GitHub Pages 静态站点
└── config/                        # 非敏感默认参数（含 stages.*）
```

## 🔄 流水线与 GitHub Actions

### 主流水线（`cron_agent_runner.yml`）

* 工作日定时 + `workflow_dispatch`
* 单 symbol 走 **3 个 Claude Code 子进程**（A → B → C），每个最多 1 次自动 retry
* 默认 `--concurrency 2 --timeout 480 --max-symbols 5`：宁可少跑几个，每个都打磨到位
* 单 symbol 典型墙钟 ~6–10 分钟；job timeout 提高到 120 分钟以覆盖最坏情况

### 站点部署

* `deploy_site.yml` 独立于 Agent 流水线，只依赖 R2 `reports/` 内容存在
* 用 `src/orchestrator/github_pages.py` 把 R2 报告同步到 Hugo content + 写 YAML front matter，然后 Hugo build

## 🛠️ 快速开始

### 1. 环境准备

需要 **Python 3.11+** 与 **Node.js**（Claude Code CLI）。

```bash
git clone https://github.com/your-username/novasignal.git
cd novasignal
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code
```

### 2. 环境变量

根目录 `.env`（已 gitignore）或 GitHub **Actions Secrets**：

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `FMP_API_KEY` | ✓ | FMP API 密钥（Premium 计划能获得最佳数据覆盖） |
| `ANTHROPIC_API_KEY` | ✓ | Anthropic API（Agent） |
| `ANTHROPIC_BASE_URL` | 可选 | 自定义 API 端点（第三方 Anthropic 兼容代理） |
| `ANTHROPIC_MODEL` | 可选 | 模型 id；**生产推荐 `claude-opus-4-x`** 以获得最佳研究深度 |
| `R2_ENDPOINT_URL` | ✓* | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | ✓* | R2 访问密钥 |
| `R2_SECRET_ACCESS_KEY` | ✓* | R2 密钥 |
| `R2_BUCKET_NAME` | 可选 | 默认 `novasignal` |

\* 生产模式上传 R2 时需要；`dev` 模式可不上传。

```env
FMP_API_KEY=your_fmp_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
ANTHROPIC_MODEL=claude-opus-4-5
R2_ENDPOINT_URL=https://xxxxxxxxxxxx.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=your_r2_access_key
R2_SECRET_ACCESS_KEY=your_r2_secret_key
R2_BUCKET_NAME=novasignal
```

> 阶段 timeout、retry 次数、QA gate 阈值在 `config/settings.yaml -> pipeline.stages.*`，按需调整。

### 3. 本地运行

```bash
python -m pytest tests/ -v

# 仅发现标的（需 FMP_API_KEY）
python -m src.orchestrator.run_pipeline --mode discovery-only

# 开发：不写 R2，三阶段全跑但结果只打 stdout（建议先 --skip-upload 试一个标的）
python -m src.orchestrator.run_pipeline --mode dev --symbols AAPL

# 生产：发现或指定标的，全套 R2 上传
python -m src.orchestrator.run_pipeline --mode production --max-symbols 3
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--symbols` | 显式指定标的（绕过发现 + 去重） |
| `--max-symbols` | 单 run 上限（默认 5，质量 > 数量） |
| `--concurrency` | 并发标的数（默认 2；三阶段流程消耗大，不建议 >3） |
| `--timeout` | 单 stage timeout 秒数（默认 480）|
| `--drafter-prompt` / `--reviewer-prompt` / `--reviser-prompt` | 三阶段 prompt 路径 |
| `--lookback-days` | IPO calendar 回溯天数 |
| `--skip-upload` | 不写 R2（即使 production 模式）|

## 🧪 输出物示例（单 symbol）

完成一次成功的 production run 后，R2 中会出现：

```
reports/2026-05-18/VIDA_report.md            # 最终研报（双语，8 章 + 资料来源 + 免责）
metrics/2026-05-18/VIDA_metrics.json         # 结构化指标
research_notes/2026-05-18/VIDA_research_notes.md   # Drafter 的研究日志（8+ 个 URL）
critique/2026-05-18/VIDA_critique.md         # Reviewer 的 10 维度评分 + Required Fixes
quality_gate/2026-05-18/VIDA_gate.json       # 三阶段执行轨迹 + 最终 QA 结果
raw_data/2026-05-18/VIDA_raw_data.json       # 输入 FMP 快照
state/2026-05-18/VIDA_state.json             # 状态机日志
```

未来可在站点上新增「分析方法论」入口暴露 `research_notes` 与 `critique`，做出**完整审计链**的产品差异化。

## ⚠️ 免责声明

输出由大模型与自动化脚本生成，仅供技术交流与回测研究。**不构成投资建议 (NFA)。** 市场有风险，投资需谨慎。
