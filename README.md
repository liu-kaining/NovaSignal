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

在根目录创建 `.env` 文件，或在 GitHub Secrets 中配置以下核心变量：

```env
FMP_API_KEY=your_fmp_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
CF_R2_ACCESS_KEY_ID=your_r2_access_key
CF_R2_SECRET_ACCESS_KEY=your_r2_secret_key
CF_R2_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com

```

### 3. 本地测试运行

你可以手动触发一次发现与分析流程：

```bash
# 测试 FMP 数据抓取与沙盒初始化
python src/orchestrator/run_pipeline.py --mode dev

```

## ⚠️ 免责声明

本项目基于大语言模型与自动化脚本生成金融分析数据，仅供技术交流与数据统计回测使用。**所有输出均不构成任何投资建议 (NFA)。** 市场有风险，投资需谨慎。
