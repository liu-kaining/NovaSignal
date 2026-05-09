# 🚀 NovaSignal (novasignal.thetamind.ai)

> 一个完全 Serverless 的 AI-Native 港美股打新与财报监控雷达。

NovaSignal 利用 GitHub Actions 作为调度引擎，通过 Python 获取 FMP (Financial Modeling Prep) 的原始财务数据，并**异步唤醒 Claude Code Agent** 在独立沙盒中完成深度的金融逻辑分析与 Markdown 研报生成。

## ✨ 核心特性

* **🤖 AI-Native 架构：** 坚决执行“数据的归数据，AI 的归 AI”。Python 只负责 API 与调度，大模型作为子进程独立负责金融推理。
* **⚡️ 零服务器 (Serverless)：** 依托 GitHub Actions 定时运转，报告与状态机日志直接持久化至 Cloudflare R2，全程零传统服务器运维成本。
* **🔄 自动重试与防崩：** 内置 `tenacity` 机制，完美容忍网络波动与大模型 API 超时。
* **📈 静态分发：** 分析结果 (Markdown) 自动同步至 GitHub Pages 渲染，零成本分发。

## 📂 目录结构

```text
novasignal/
├── .github/workflows/    # Actions 自动化流水线
├── src/                  # Python 核心调度代码
│   ├── orchestrator/     # 沙盒管理与 Agent 唤醒引擎
│   ├── fetchers/         # FMP 接口交互
│   └── storage/          # Cloudflare R2 存储同步
├── prompts/              # AI 的“基因库” (分析标准与指令)
└── config/               # 全局环境与阈值配置

```

## 🛠️ 快速开始

### 1. 环境准备

确保你的环境中已安装 Python 3.11+ 和 Node.js（用于运行 Claude Code CLI）。

```bash
# 克隆仓库
git clone [https://github.com/your-username/novasignal.git](https://github.com/your-username/novasignal.git)
cd novasignal

# 安装 Python 依赖
pip install -r requirements.txt

# 确保已安装 Claude Code
npm install -g @anthropic-ai/claude-code

```

### 2. 环境变量配置

在根目录创建 `.env` 文件（已在 `.gitignore` 中），或在 GitHub 仓库 Settings → Secrets and variables → Actions 中配置：

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `FMP_API_KEY` | ✓ | Financial Modeling Prep API 密钥 |
| `ANTHROPIC_API_KEY` | ✓ | Anthropic API 密钥（Agent 调用） |
| `ANTHROPIC_BASE_URL` | 可选 | 自定义 Anthropic API 端点（代理/区域端点），留空使用官方 API |
| `ANTHROPIC_MODEL` | 可选 | 指定 Claude 模型（如 `claude-sonnet-4-6`），留空使用 CLI 默认值 |
| `R2_ENDPOINT_URL` | ✓ | Cloudflare R2 端点，格式：`https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | ✓ | R2 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | ✓ | R2 Secret Access Key |
| `R2_BUCKET_NAME` | 可选 | R2 桶名，默认 `novasignal` |

本地 `.env` 文件示例：

```env
FMP_API_KEY=your_fmp_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
R2_ENDPOINT_URL=https://xxxxxxxxxxxx.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=your_r2_access_key
R2_SECRET_ACCESS_KEY=your_r2_secret_key
R2_BUCKET_NAME=novasignal
```

> **注意：** `config/settings.yaml` 中存放非敏感的默认参数（超时、重试策略、阈值等），无需修改即可使用。

### 3. 本地测试运行

```bash
# 运行单元测试（不需要任何 API 密钥）
python -m pytest tests/ -v

# 仅测试 FMP 数据发现（需要 FMP_API_KEY）
python -m src.orchestrator.run_pipeline --mode discovery-only

# 开发模式：跳过 R2 上传，结果打印到 stdout
python -m src.orchestrator.run_pipeline --mode dev --symbols AAPL TSLA

# 生产模式：全流程（需要全部环境变量）
python -m src.orchestrator.run_pipeline --mode production
```

## ⚠️ 免责声明

本项目基于大语言模型与自动化脚本生成金融分析数据，仅供技术交流与数据统计回测使用。**所有输出均不构成任何投资建议 (NFA)。** 市场有风险，投资需谨慎。
