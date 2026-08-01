![Push图片](doc/img/open/ai-codereview-cartoon.png)

# AI-Codereview-Gitlab-Opencode

基于 [sunmh207/AI-Codereview-Gitlab](https://github.com/sunmh207/AI-Codereview-Gitlab) 演进的 Agent Review 版本，保留原有多平台 AI Code Review 能力，并支持 OpenCode Serve、Codex CLI、Claude CLI 和 Pi CLI 四种显式后端。

[开源版](README.md) | 
[Pro版](doc/pro.md)

## 项目简介

本项目是一个基于大模型的自动化代码审查工具，帮助开发团队在代码合并或提交时，快速进行智能化的审查(Code Review)，提升代码质量和开发效率。

## 功能

- 🚀 多模型支持
  - 兼容 DeepSeek、ZhipuAI、OpenAI、Anthropic、通义千问 和 Ollama，想用哪个就用哪个。
- 📢 消息即时推送
  - 审查结果一键直达 钉钉、企业微信 或 飞书，代码问题无处可藏！
- 📅 自动化日报生成
  - 基于 GitLab & GitHub & Gitea Commit 记录，自动整理每日开发进展，谁在摸鱼、谁在卷，一目了然 😼。
- 📊 可视化 Dashboard
  - 集中展示所有 Code Review 记录，项目统计、开发者统计，数据说话，甩锅无门！
- 🎭 Review Style 任你选
  - 专业型 🤵：严谨细致，正式专业。
  - 讽刺型 😈：毒舌吐槽，专治不服（"这代码是用脚写的吗？"）
  - 绅士型 🌸：温柔建议，如沐春风（"或许这里可以再优化一下呢~"）
  - 幽默型 🤪：搞笑点评，快乐改码（"这段 if-else 比我的相亲经历还曲折！"）
- 🤖 Agentic Review 模式（可选）
  - LLM 拥有工具调用能力（`read_file` / 沙箱 `run_command`），
    可在本地克隆的代码库内自主探索，产出更全面的 review 结果。
  - shell 默认仅允许读类命令（`ls` / `cat` / `grep` / `find` / `git log` …），
    沙箱 + 路径越界 + 30s 超时三重防护。
  - 任意阶段失败（clone / fetch / LLM / 工具调用）自动降级回 `diff_only`，
    保证至少返回与原版一致的 review。
  - 详细配置与开销说明见下方 [Agentic Review Mode](#agentic-review-mode-可选)
- 🤖 External Agent Review 集成
  - 支持 OpenCode Serve、Codex CLI、Claude CLI，按配置明确选择一个后端
  - 当收到 GitHub/GitLab/Gitea PR/MR webhook 事件时，自动触发 Agent Review
  - 本地仓库优先，找不到时按配置 clone；每次执行使用一次性 worktree
  - 可与内置 LLM Review 功能并行使用或独立使用

**效果图:**

![MR图片](doc/img/open/mr.png)

![Note图片](doc/img/open/note.jpg)

![Dashboard图片](doc/img/open/dashboard.jpg)

## 原理

当用户在 GitLab 上提交代码（如 Merge Request 或 Push 操作）时，GitLab 将自动触发 webhook
事件，调用本系统的接口。系统随后通过第三方大模型对代码进行审查，并将审查结果直接反馈到对应的 Merge Request 或 Commit 的
Note 中，便于团队查看和处理。

![流程图](doc/img/open/process.png)

## 部署

### 方案一：Docker 部署

**1. 准备环境文件**

- 克隆项目仓库：
```aiignore
git clone https://github.com/wufei-png/AI-Codereview-Gitlab-Opencode.git
cd AI-Codereview-Gitlab-Opencode
```

- 创建配置文件：
```aiignore
cp conf/.env.dist conf/.env
```

- 编辑 conf/.env 文件，配置以下关键参数：

```bash
#大模型供应商配置,支持 zhipuai , openai , deepseek 和 ollama
LLM_PROVIDER=deepseek

#DeepSeek
DEEPSEEK_API_KEY={YOUR_DEEPSEEK_API_KEY}

#支持review的文件类型(未配置的文件类型不会被审查)
SUPPORTED_EXTENSIONS=.java,.py,.php,.yml,.vue,.go,.c,.cpp,.h,.js,.css,.md,.sql

#钉钉消息推送: 0不发送钉钉消息,1发送钉钉消息
DINGTALK_ENABLED=0
DINGTALK_WEBHOOK_URL={YOUR_WDINGTALK_WEBHOOK_URL}

#Gitlab配置
GITLAB_ACCESS_TOKEN={YOUR_GITLAB_ACCESS_TOKEN}

#OpenCode Agent Review配置（可选）
#兼容旧配置；新配置见“配置 External Agent Review”
OPENCODE_ENABLED=0  # 0关闭，1开启
OPENCODE_API_URL=http://localhost:4096  # OpenCode Serve API地址
OPENCODE_AGENT_NAME=code-reviewer  # Agent名称
# 如果 OpenCode 服务器启用了认证，需要配置以下两项
# OPENCODE_SERVER_USERNAME=opencode
# OPENCODE_SERVER_PASSWORD=your-password

#LLM Review开关（设置为0则不通过内置LLM进行Code Review）
LLM_REVIEW_ENABLED=1  # 0关闭，1开启
```

**2. 启动服务**

```bash
docker-compose up -d
```

**3. 验证部署**

- 主服务验证：
  - 访问 http://your-server-ip:5001
  - 显示 "The code review server is running." 说明服务启动成功。
- Dashboard 验证：
  - 访问 http://your-server-ip:5002
  - 看到一个审查日志页面，说明 Dashboard 启动成功。

### 方案二：本地Python环境部署

**1. 获取源码**

```bash
git clone https://github.com/wufei-png/AI-Codereview-Gitlab-Opencode.git
cd AI-Codereview-Gitlab-Opencode
```

**2. 安装依赖**

使用 Python 环境（建议使用虚拟环境 venv）安装项目依赖(Python 版本：3.10+):

```bash
pip install -r requirements.txt
```

**3. 配置环境变量**

同 Docker 部署方案中的.env 文件配置。

**4. 启动服务**

- 启动API服务：

```bash
python api.py
```

- 启动Dashboard服务：

```bash
streamlit run ui.py --server.port=5002 --server.address=0.0.0.0
```

### 配置 GitLab Webhook

#### 1. 创建Access Token

方法一：在 GitLab 个人设置中，创建一个 Personal Access Token。

方法二：在 GitLab 项目设置中，创建Project Access Token

#### 2. 配置 Webhook

在 GitLab 项目设置中，配置 Webhook：

- URL：http://your-server-ip:5001/review/webhook
- Trigger Events：勾选 Push Events 和 Merge Request Events (不要勾选其它Event)
- Secret Token：上面配置的 Access Token(可选)

**备注**

1. Token使用优先级
  - 系统优先使用 .env 文件中的 GITLAB_ACCESS_TOKEN。
  - 如果 .env 文件中没有配置 GITLAB_ACCESS_TOKEN，则使用 Webhook 传递的Secret Token。
2. 网络访问要求
  - 请确保 GitLab 能够访问本系统。
  - 若内网环境受限，建议将系统部署在外网服务器上。

### 配置消息推送

#### 1.配置钉钉推送

- 在钉钉群中添加一个自定义机器人，获取 Webhook URL。
- 更新 .env 中的配置：
  ```
  #钉钉配置
  DINGTALK_ENABLED=1  #0不发送钉钉消息，1发送钉钉消息
  DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx #替换为你的Webhook URL
  ```

企业微信和飞书推送配置类似，具体参见 [常见问题](doc/faq.md)

### 配置 OpenCode Agent Review

OpenCode Agent Review 是一个可选的代码审查功能，可以与内置的 LLM Review 功能并行使用或独立使用。

#### 1. 启用 OpenCode Agent Review

- 确保已部署 OpenCode Serve 服务（参考 OpenCode 官方文档），例如：`opencode serve --hostname 0.0.0.0 --port 4096`
- 更新 .env 中的配置：
  ```bash
  # OpenCode Agent Review配置
  OPENCODE_ENABLED=1  # 0关闭，1开启
  OPENCODE_API_URL=http://127.0.0.1:4096  # 替换为你的OpenCode Serve API地址
  OPENCODE_AGENT_NAME=code-reviewer  # 替换为你的Agent名称
  
  # 如果 OpenCode 服务器启用了认证，需要配置以下两项
  OPENCODE_SERVER_USERNAME=opencode
  OPENCODE_SERVER_PASSWORD=your-password
  ```
- [opencode示例配置](./opencode)

OpenCode Serve 需要能访问本服务传入的临时 job 目录；如果 OpenCode 运行在另一个容器中，请把 `worktree_parent` 映射为相同路径，或通过 `AGENT_WORKTREE_PARENT` 配置双方一致的路径。服务会把 disposable source clone 和 canonical skill 副本放入 job 目录，并生成一次性 `opencode.json`，任务结束后随 job 目录清理。

#### 2. 功能说明

- 当 webhook 收到 GitHub/GitLab/Gitea 的 PR/MR 事件时，如果 `OPENCODE_ENABLED=1`，系统会自动调用 OpenCode Serve API 创建 session 并发送 review 请求
- OpenCode Review 和内置 LLM Review 可以同时启用，两者互不影响
- 如果只需要使用 OpenCode Review，可以设置 `LLM_REVIEW_ENABLED=0` 来关闭内置 LLM Review

### 配置 External Agent Review

这是当前 OpenCode/CLI Agent 部分的核心入口。Webhook 路由、服务代码和 `opencode/opencode.json` 都会使用同一份审查 skill：[`skills/review-agent/SKILL.md`](skills/review-agent/SKILL.md)。它定义仓库上下文、缺陷优先审查标准、worktree 生命周期、默认自动修复和平台 CLI 交付要求；`opencode/prompts/code-reviewer_v2.md` 已删除，旧的分散提示词不再作为运行时入口。

后端不自动切换，也不 fan-out。选择一个后端：

```bash
AGENT_REVIEW_ENABLED=1
AGENT_BACKEND=opencode   # opencode | codex | claude | pi
AGENT_REVIEW_CONFIG=conf/agent_repos.yml
AGENT_SHARED_REVIEW_SKILL_PATH=  # 可选，覆盖共享 skill 绝对路径
```

`OPENCODE_ENABLED=1` 仍可作为旧配置的兼容开关；启用 External Agent 后仍必须配置 webhook secret/signature 校验。显式设置 `AGENT_REVIEW_ENABLED` 后以它为准。Codex、Claude、Pi 后端分别调用本机 `codex exec`、`claude -p`、`pi --print`，OpenCode 后端调用 OpenCode Serve API。项目不负责安装 CLI、登录、创建 token 或维护认证/授权状态；运行前请在所选 backend 的实际执行环境中完成对应 Agent CLI 和平台 CLI 配置——本地 backend 是 worker 环境，OpenCode 是 Serve 环境。平台交付由 canonical skill 指示 Agent 使用 `glab`、`gh`、`tea` 或配置的平台等价命令完成，不使用 MCP。

Webhook 只把规范化任务写入 SQLite durable queue，不在 Web 进程中启动 Agent。至少启动一个独立 worker：

```bash
python -m biz.agent.worker
```

worker 默认使用与 Web 服务相同的系统账户，也可以由部署系统以独立低权限账户启动。`worker_concurrency` 只设置全局并发；同一个 MR/PR 的 revision 会串行处理。

worker 必须能在 `PATH` 中找到所选 Agent CLI 和平台 CLI；服务只检查可执行文件是否存在，不探测认证或权限。默认 Docker 镜像不替用户安装这些 CLI 或认证环境。

安全边界说明：External Agent 是被信任的执行者，当前集成用进程参数、临时 job 目录和 disposable source clone 限制正常路径，但不会伪造 Claude/Codex/OpenCode 的 OS 级沙箱。Agent 为了使用已认证的 `glab` / `gh` / Gitea CLI，可能继承操作者提供的 CLI 配置、Git credential helper 或 SSH agent；请在专用低权限用户、容器或仅挂载 job workspace 的 worker 中运行，不要让不可信仓库使用宿主机高权限凭据。项目不负责创建、登录、刷新或托管这些凭据。

当本地 `repo_roots` 没有匹配而需要 clone 私有仓库时，Git clone 还必须具备独立的 Git 凭据：可使用 Webhook 提供的 SSH remote/本机 SSH agent、Git credential helper，或配置 `GITHUB_ACCESS_TOKEN` / `GITLAB_ACCESS_TOKEN` / `GITEA_ACCESS_TOKEN`。`gh`/`glab` 登录状态本身不保证 Git 已配置 credential helper；本项目不读取 CLI 配置、不提取 token，也不负责登录过程。配置本地 `repo_roots` 可以完全绕过这一步。

如果只启用 External Agent Review，可同时设置 `LLM_REVIEW_ENABLED=0`；此时 Webhook 不要求项目配置 GitHub/GitLab/Gitea API token，平台操作由已认证的 CLI 完成。若保留内置 LLM Review，则仍需按原有配置提供对应平台 token。

启用 External Agent Review 后，Webhook 必须配置对应的 `GITHUB_WEBHOOK_SECRET`、`GITLAB_WEBHOOK_SECRET` 或 `GITEA_WEBHOOK_SECRET`，服务会校验 GitLab token header 或 GitHub/Gitea HMAC 签名；GitLab Standard Webhooks 还可配置 `GITLAB_WEBHOOK_SIGNING_TOKEN` 校验 `webhook-id`/`webhook-timestamp`/`webhook-signature`。未通过校验不会创建 Agent Job。不要把平台 API token 当作新的 webhook secret 使用；旧 access token 回退只有显式设置 `AGENT_ALLOW_ACCESS_TOKEN_WEBHOOK_FALLBACK=1` 才启用。

仓库发现与临时目录由 `conf/agent_repos.yml` 控制：

```yaml
repo_roots:
  "https://gitlab.example.com/team/": "/srv/repos/team"
  # 也可以直接配置完整项目 URL：
  # "https://gitlab.example.com/team/payment.git": "/srv/repos/team/payment"
discovery_max_depth: 3
allowed_remote_hosts: [gitlab.example.com]
clone_parent: data/agent-clones
clone_cleanup: always       # always | never | on_success
worktree_parent: data/agent-worktrees
shared_review_skill: skills/review-agent/SKILL.md
backend: opencode
```

`allowed_remote_hosts` 非空时是严格白名单；未填写时才从 `repo_roots`、`GITLAB_URL`、`GITHUB_URL` 和 `GITEA_URL` 推导默认主机。若请求包含 fork，source、target 和 review URL 的主机都必须通过白名单。

系统先按远程 URL 推导直接路径并确认 `origin` 一致；找不到时只在对应 `repo_roots` 下按 `discovery_max_depth` 递归查找 Git 仓库并确认 remote。仍找不到才 clone 到 `clone_parent`。Agent 自己在独立 `worktree_parent` 子目录中选择 worktree 路径和分支细节；worktree 固定清理，clone 默认也清理，可用 `clone_cleanup` 保留。服务在 Agent 运行前分别从 source 和 target 项目 fetch 最新分支，记录 `SOURCE_REVISION` 与 `TARGET_REVISION`，并以 merge base 审查完整 MR diff；fork 中的同名 target 分支不会替代 upstream。后续 source revision 仍做完整正确性审查，但用上次已交付 revision 到当前 revision 的范围突出新增问题，并更新每个 MR/PR 唯一的 Rolling Review Note。Agent 实际拿到的是 job 目录内的 disposable source clone 和 skill 副本，原始本地仓库不会作为 CLI 的可写目录暴露。

自动修复默认开启，规则位于共享 skill 的 `## Auto-fix policy (enabled by default)`。修复会创建基于 `SOURCE_REVISION`、目标为原始 source project/source branch 的 stacked fix MR/PR；不会默认创建指向原始 target branch 的独立替代变更。如果只想审查、不自动修复，请在运行前使用不含该段的 skill 副本；不要让 Agent 在 job 中修改 canonical skill。

默认 `AGENT_BACKEND_TIMEOUT=-1`，只表示 Agent 执行不限时；clone/fetch、OpenCode 会话建立和清理仍有独立正数 timeout。其他配置包括 `AGENT_WORKER_CONCURRENCY`、`AGENT_WORKER_SHUTDOWN_GRACE`、`AGENT_JOB_RETENTION_DAYS` 和可选的 `AGENT_RESULT_MAX_BYTES`。结果默认不限制大小；显式设置上限后保留头尾并记录截断。Job/结果默认保留 90 天。

SQLite queue 在 webhook hints 和实际 fetch 后的 source/target revision 两层做幂等。Agent 启动前的临时基础设施失败最多指数退避重试三次；Agent 一旦启动就不自动重试，避免重复交付。Execution Status 与 Delivery Status 分离：backend 退出 0 得到 `completed`，只有有效的平台原生 delivery receipt 才得到 `confirmed`。

Lease heartbeat 和 token fencing 会阻止正常的过期任务继续更新 job 状态；若宿主进程本身失联但其外部 Agent 子进程仍未退出，无法从 SQLite 中撤销已经发出的平台 CLI/OpenCode 网络副作用。生产部署应使用 backend timeout、专用 worker 和平台侧幂等/人工检查处理这一极端残余风险。

## 常见问题

**1.如何对整个代码库进行Review?**

可以通过命令行工具对整个代码库进行审查。当前功能仍在不断完善中，欢迎试用并反馈宝贵意见！具体操作如下：

```bash
python -m biz.cmd.review
```

运行后，请按照命令行中的提示进行操作即可。

**2.其它常见问题**

参见 [常见问题](doc/faq.md)

## Agentic Review Mode (可选)

`REVIEW_STRATEGY` 环境变量切换两种 review 策略：

- `diff_only`（默认）：仅对 diff 做 review，行为与原版完全一致。
- `agentic`：LLM 拥有工具调用能力（read_file / 沙箱 shell），
  可在本地克隆的代码库内自主探索，产出更全面的 review 结果。

启用 agentic 模式：

```bash
REVIEW_STRATEGY=agentic
REPO_CACHE_DIR=/var/data/repo_cache   # 可选，默认 data/repo_cache/
AGENT_MAX_ITERATIONS=20               # 可选，默认 20
```

agentic 模式会按需在 `REPO_CACHE_DIR` 下克隆/更新目标项目（约 10MB~2GB / 项目）。
任意阶段失败（clone / fetch / LLM / 工具调用异常）都会自动降级回 `diff_only`，
保证至少返回与原版一致的 review。

agentic 模式的额外开销：

- 磁盘：建议预留 ≥ 50GB
- 内存：单次 session 峰值 ~500MB
- Token：单次 review 平均 5k - 50k tokens（diff_only 的 3 - 10 倍）
- 时延：30s~5min / review

⚠️ shell 工具有沙箱（命令白名单 + 黑名单 + 路径越界检查 + 30s 超时），
默认只允许读类命令；如需放开请通过 `AGENT_SHELL_ALLOWLIST` / `AGENT_SHELL_BLOCKLIST` 调整。

## 相关项目

### 1. Code Review Pro 版

功能更丰富的 AI Code Review 版本。

项目介绍与使用说明：[Code Review Pro 版](doc/pro.md)

快速安装命令：

```bash
curl -fsSL https://raw.githubusercontent.com/sunmh207/AI-Codereview-Gitlab/refs/heads/main/scripts/pro/install.sh | bash
```

### 2. Entire Dashboard

如果你正在使用 AI Agent 开发工具 (如: Cursor、Claude Code、Codex ...)，并希望对人机交互过程进行全面的记录与回溯分析，推荐使用 [Entire Dashboard](https://github.com/sunmh207/entire-dashboard)。该项目提供了完整的人机交互记录与可视化分析功能，可帮助你深入理解 AI Agent 的使用模式，优化交互体验，提升开发效率。

## 交流

若本项目对您有帮助，欢迎 Star ⭐️ 或 Fork。 有任何问题或建议，欢迎提交 Issue 或 PR。

也欢迎加微信/微信群，一起交流学习。

<p float="left">
  <img src="doc/img/open/wechat.jpg" width="400" />
  <img src="doc/img/open/wechat_group.jpg" width="400" /> 
</p>

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=wufei-png/AI-Codereview-Gitlab-Opencode&type=Timeline)](https://www.star-history.com/#wufei-png/AI-Codereview-Gitlab-Opencode&Timeline)
