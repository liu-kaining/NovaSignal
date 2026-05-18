# 🚀 NovaSignal (novasignal.thetamind.ai)

> 完全 Serverless 的 AI-Native 港美股打新与财报监控雷达。

NovaSignal 用 **GitHub Actions** 做调度，用 **Python + FMP (Financial Modeling Prep)** 拉取并结构化财务与市场数据，再**异步拉起 Claude Code Agent** 在独立沙盒里按 `prompts/` 中的合约生成 Markdown 研报与结构化 `metrics`。

## ✨ 核心特性

* **AI-Native 分层：** 「数据在 Python、推理在 Agent」。编排层只做 API、重试、沙盒与 R2 写入；Claude 通过子进程异步执行，不阻塞事件循环上的其它并发标的（`asyncio` + `create_subprocess_exec` + `wait_for`）。
* **FMP 深度增强：** 每轮流水线会先做一次 **全局预取**（IPO 监管披露/招股书列表、宏观利率/经济日历/指标快照、行业与板块表现、主要指数报价等），再按标的合并进 **`raw_data`**（财报、可比公司、卖方、持股与治理、SEC、新闻、板块语境等）。生产模式下这些内容会**持久化到 R2**，便于审计与复现。
* **R2 数据布局（生产模式）：**
  * `reports/{YYYY-MM-DD}/{SYMBOL}_report.md` — 研报正文（同步到 Hugo / GitHub Pages 的也是这一前缀）。
  * `metrics/{YYYY-MM-DD}/{SYMBOL}_metrics.json` — 结构化评分与信号字段。
  * `raw_data/{YYYY-MM-DD}/{SYMBOL}_raw_data.json` — 该标的当次 Agent 输入侧完整 FMP 增强快照（含标的专属字段 + 与当次 run 一致的全球/监管上下文摘要）。
  * `raw_data/_shared/{YYYY-MM-DD}/fmp_prefetch_bundle.json` — **每轮一次**的全局预取包（IPO 列表 + `global_market_context`）；同一天多次运行会覆盖同名对象，单标的 `raw_data` 内仍保留合并后的副本以保证单文件可复现。
* **零服务器 (Serverless)：** 无常驻机；密钥走 GitHub Secrets，对象存储走 Cloudflare R2。
* **静态站点：** `deploy_site` 工作流只从 R2 拉取 **`reports/`** 写入 Hugo；`raw_data/` 与 `metrics/` 留在桶内供下载或下游系统使用，默认不上站。
* **幂等与防刷：** 自动发现模式下会按 R2 已有 `reports/` 过滤已分析标的，避免重复烧 Token（显式 `--symbols` 时仍按你指定的列表跑）。

## 📂 目录结构

```text
novasignal/
├── .github/workflows/    # Actions：定时/手动跑流水线、站点部署
├── src/
│   ├── orchestrator/     # run_pipeline、沙盒、异步 Agent、Hugo 同步
│   ├── fetchers/         # FMP 客户端与限流/缓存
│   └── storage/          # R2 客户端（上传/列举/下载）
├── prompts/              # Agent 产品合约（如 ipo_v1_template.md）
├── hugo/                 # GitHub Pages 静态站点
└── config/               # 非敏感默认参数
```

## 🔄 流水线与 GitHub Actions

* **默认工作流**（`cron_agent_runner.yml`）：工作日定时 + `workflow_dispatch`，`--concurrency 4`、`--timeout 300`（**每个标的**沙盒内 Agent 超时秒数）。增强后单标的 FMP 调用更多，**总墙钟时间会拉长**；若一次性标的很多或 Agent 经常触顶超时，可适当提高 **`timeout-minutes`（整 job）** 或 CLI **`--timeout`**。
* **Claude 不会「整 job 同步卡死在整个进程上」**：并发标的之间仍由信号量调度；每个标的内部对 CLI 使用异步子进程。R2 的 `put_object` 仍为同步调用，但相对 Agent 耗时通常很短。
* **部署站点**：与跑 Agent 分离；站点只依赖 `reports/` 已在 R2 中。

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
| `FMP_API_KEY` | ✓ | FMP API 密钥（部分端点为 Premium 计划能力，见 FMP 文档） |
| `ANTHROPIC_API_KEY` | ✓ | Anthropic API（Agent） |
| `ANTHROPIC_BASE_URL` | 可选 | 自定义 API 端点 |
| `ANTHROPIC_MODEL` | 可选 | 模型 id，留空则 CLI 默认 |
| `R2_ENDPOINT_URL` | ✓* | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | ✓* | R2 访问密钥 |
| `R2_SECRET_ACCESS_KEY` | ✓* | R2 密钥 |
| `R2_BUCKET_NAME` | 可选 | 默认 `novasignal` |

\* 生产模式上传 R2 时需要；`dev` 模式可不上传。

```env
FMP_API_KEY=your_fmp_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
R2_ENDPOINT_URL=https://xxxxxxxxxxxx.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=your_r2_access_key
R2_SECRET_ACCESS_KEY=your_r2_secret_key
R2_BUCKET_NAME=novasignal
```

> 敏感阈值与重试等默认在 `config/settings.yaml`，一般无需改。

### 3. 本地运行

```bash
python -m pytest tests/ -v

# 仅发现标的（需 FMP_API_KEY）
python -m src.orchestrator.run_pipeline --mode discovery-only

# 开发：不写 R2，结果打 stdout
python -m src.orchestrator.run_pipeline --mode dev --symbols AAPL

# 生产：发现或指定标的 → raw_data/metrics/report 上传 R2
python -m src.orchestrator.run_pipeline --mode production
```

常用参数：`--symbols`、`--concurrency`、`--timeout`、`--lookback-days`、`--skip-upload`（即使 production 也跳过上传，用于联调）。

## ⚠️ 免责声明

输出由大模型与自动化脚本生成，仅供技术交流与回测研究。**不构成投资建议 (NFA)。** 市场有风险，投资需谨慎。
