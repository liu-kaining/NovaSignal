# 🚀 NovaSignal (novasignal.thetamind.ai)

**产品需求与技术架构白皮书 v1.0**

## 一、 执行摘要与产品愿景 (Executive Summary)

### 1.1 产品定位

**NovaSignal** 是 ThetaMind 旗下的全生命周期金融资产监控与情报工厂。它是一个完全 **Serverless（无服务器化）** 且 **AI-Native** 的二级市场引擎。系统旨在自动化监控全球（美股、港股）IPO 动态与财报发布，利用 Agent 完成高密度的研报生成，并通过长周期的“回测-归因”实现投资策略的自我进化。

### 1.2 商业化演进路线

* **阶段一：广撒网与资产沉淀（0-6 个月）**
建立全量 IPO 与财报数据库，通过标准化的 AI 报告获取自然搜索流量，同时为 AI 积累“预测 vs 真实”的底层数据资产。
* **阶段二：模型对齐与自进化（6-12 个月）**
启动“每日复盘”机制，基于真实股价表现纠正 AI 评分偏差，沉淀核心护城河。
* **阶段三：精准避雷与信号订阅（1 年以后）**
为专业投资者、打新基金提供经过历史胜率背书的高分信号、破发预警及量化 API 服务。

---

## 二、 产品需求文档 (PRD)

### 2.1 核心业务链路 (Core Pipelines)

系统由四个完全解耦的流水线构成：

1. **情报发现线 (Discovery Pipeline)**
* **需求：** 每日/每小时轮询 FMP API，捕获状态变更的资产（如新增 S-1 申报、即将发布财报的标的）。
* **输出：** 生成待处理任务队列，流转至下一环节。


2. **AI 解析线 (Analysis Pipeline)**
* **需求：** 将枯燥的财务 JSON 转化为标准化的多维分析报告。
* **输出：** 生成前端展示用的 `[symbol]_report.md` 以及后端回测用的 `[symbol]_metrics.json`。


3. **渲染与交互线 (Rendering & Interaction)**
* **需求：** 静态站点渲染（GitHub Pages），按“高分、板块、美/港股”聚合标签，展示公司全生命周期的时间轴。
* **交互：** 采用 Giscus 承接用户评论，Umami 追踪用户数据，Cloudflare Workers (可选) 承载用户看好/看空投票。


4. **自学闭环线 (Evolution Pipeline)**
* **需求：** T+30 抓取实际股价，与初始预测计算偏差度（Loss）。当判断连续出现 3-5 次显著偏差时，触发修正机制。
* **进化：** 自动重写 Prompt 规则或调整权重参数，并自动 Commit 至主分支。



---

## 三、 技术架构白皮书 (Technical Architecture)

### 3.1 核心设计哲学

* **数据的归数据，AI 的归 AI：** 坚决执行关注点分离。Python 负责 API 通信与流程编排，Claude Code 负责纯粹的认知计算。
* **状态机与异步重试：** 拥抱网络波动与大模型超时，任务状态必须持久化。
* **零传统服务器：** 依托边缘节点和 CI/CD 完成所有计算，杜绝 ECS/RDS 运维成本。

### 3.2 核心技术栈矩阵

| 架构层级 | 组件选型 | 核心职责 |
| --- | --- | --- |
| **调度与计算引擎** | GitHub Actions | 定时调度 (Cron)、并发任务执行与 Python 容器环境。 |
| **数据源 (Data)** | Financial Modeling Prep | 全量提供美港股 IPO 日历、财务报表、实时报价。 |
| **执行引擎 (AI)** | Python 3.11 + Claude Code | Python 作为 Orchestrator，异步唤醒 Claude Agent 进程。 |
| **存储 (Data Lake)** | Cloudflare R2 | 永久存储 Markdown 报告、JSON 快照及任务状态日志。 |
| **分发与展示** | GitHub Pages + Jekyll | 静态站构建，零成本全球 CDN 加速。 |

### 3.3 核心引擎机制：异步沙盒模式 (Sandbox Pattern)

为确保系统健壮性，AI 解析线必须严格遵循以下执行生命周期：

1. **沙盒搭建：** Python 读取队列，为目标标的创建临时隔离工作区 `/tmp/workspace_[symbol]/`。下载 `raw_data.json` 与当前 `ipo_prompt.md` 模板至该目录。
2. **异步唤醒：** Python 使用 `asyncio.create_subprocess_exec` 启动 `claude code` 进程，将工作目录限制在沙盒内。赋予大模型自主读写本地文件的能力。
3. **容错机制：** 采用 `tenacity` 库。若 Claude 进程超时（设限 300s）或输出异常，Python 拦截错误并执行**指数退避重试**（最高 3 次）。
4. **回收销毁：** 任务成功后，Python 提取 `report.md` 与 `metrics.json` 同步至 Cloudflare R2，随后销毁临时沙盒。

### 3.4 仓库目录结构规范

```text
novasignal/
├── .github/
│   └── workflows/
│       ├── cron_discovery.yml    # 【发现】每小时拉取 FMP
│       ├── cron_agent_runner.yml # 【执行】消费队列，异步唤醒沙盒
│       ├── cron_evolution.yml    # 【自省】回测与 Prompt 自动修正
│       └── deploy_site.yml       # 【发布】R2 同步与静态构建
├── src/
│   ├── orchestrator/             # 控制平面 (Python)
│   │   ├── sandbox_manager.py    # 沙盒创建与销毁
│   │   └── async_runner.py       # subprocess 调用与 tenacity 重试
│   ├── fetchers/                 # FMP 数据面 (重试机制与鉴权)
│   ├── storage/                  # Cloudflare R2 接口封装
│   └── evolution/                # 偏差计算与自省引擎
├── prompts/                      # 【系统基因库】
│   ├── ipo_v1_template.md        
│   └── meta_review_template.md   
├── config/
│   └── settings.yaml             # 阈值配置与全局变量
└── site/                         # 静态站源码

```

---

## 四、 AI 初始化编码指令 (Initialization Prompt)


```markdown
# Role & Context
你是一个顶级的 Python 云原生架构师和 AI Agent 工程师。我们要从零开始构建一个名为 **NovaSignal** (部署于 `novasignal.thetamind.ai`) 的金融情报流水线项目。

# Project Architecture Rules
1. **边界清晰：** 数据的归数据，AI 的归 AI。
2. **Python 职责：** 负责调用 FMP (Financial Modeling Prep) API 抓取数据，管理本地沙盒目录的文件读写，与 Cloudflare R2 进行对象存储交互。
3. **Agent 职责：** Python 将通过 `asyncio` 异步启动子进程 (subprocess) 调用 `claude code` CLI，在隔离的临时目录中让大模型读取 JSON 和 prompt，自行生成 `.md` 和 `.json` 结果。
4. **强健性：** 必须使用 `tenacity` 库对 API 请求和 Agent 唤醒进行包含指数退避的自动重试。
5. **部署：** 全程无服务器，基于 GitHub Actions 运行。

# Task: Milestone 1 - Foundation & Fetcher
当前任务是建立项目基建。请严格执行以下步骤：

1. **构建目录树：** 根据最佳实践创建项目目录结构，包含 `.github/workflows`, `src/fetchers`, `src/orchestrator`, `src/storage`, `prompts/`, `config/`。
2. **依赖管理：** 生成 `requirements.txt`，必须包含 `requests`, `aiohttp`, `boto3`, `tenacity`, `pyyaml` 等基础库。
3. **核心数据流开发：**
   - 在 `src/fetchers/` 下编写 `fmp_client.py`。
   - 实现初始化鉴权（从环境变量读取 `FMP_API_KEY`）。
   - 实现基础方法 `get_ipo_calendar(from_date, to_date)`，要求具备基于 `tenacity` 的网络重试逻辑和基础的错误捕获。
4. **日志规范：** 所有的 Python 脚本需配置标准 `logging`，输出关键动作的 DEBUG/INFO 信息。

请充分思考模块解耦，然后直接生成对应的目录结构和上述要求的 Python 代码。不要编写任何与 UI 或前端相关的代码。

```
