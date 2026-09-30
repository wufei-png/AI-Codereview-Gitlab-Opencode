# Agent worker 运行与恢复

当前支持受控自用/团队内网，推荐主机运行。设计取舍见 [ADR-0007](../adr/0007-trusted-agent-reliability-boundary.md)，分阶段验收见 [实现计划](../plans/2026-09-30-reliability-implementation-plan.md)。

## 主机启动

使用 Python 3.11+，在项目根目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp conf/.env.dist conf/.env
```

填写各平台 webhook secret；仅用外部 Agent 时设 `AGENT_REVIEW_ENABLED=1`、`LLM_REVIEW_ENABLED=0`、`AGENT_BACKEND=opencode|codex|claude|pi` 中的一个实际值。API、UI、worker 均从项目绝对路径加载 `conf/.env`，不覆盖 shell 已导出的变量；相对 Agent 配置路径仍相对于项目根。使用同一操作者账户或明确共享目录/文件权限。

```bash
python -m biz.agent.worker --check
python api.py
streamlit run ui.py --server.port=5002
python -m biz.agent.worker
```

后三个命令分别在独立终端/进程管理器中运行。`--check` 只检查配置、目录/SQLite 写权限、skill、Git 和所选本地 Agent CLI；不会 claim、reap、retry 或调用模型。它可能创建目录和初始化/升级 DB schema。平台 CLI 可用性仅报告，实际领取 job 时再检查该 provider；认证、仓库权限与外部连接均未验证。

本地 backend 的 Agent CLI 与 `glab`/`gh`/配置的 Gitea CLI 在 worker 账户中预先安装、登录，Git fetch 的认证另行配置。OpenCode 所需模型与平台 CLI 在 Serve 账户中配置，worker 不要求本机安装平台 CLI。项目不安装或提取登录凭据。保持 CLI 的原生文本/JSON/JSONL，平台评论由 Agent 发布。

服务准备 exact Source Revision 的 worktree，三种本地 CLI 从 worktree 执行。OpenCode 以 job 根加载生成的 `opencode.json`/skill，prompt 指向其中的 `worktree/`。Serve 必须能读取**相同绝对路径**的 job 根；仅能联网访问 API 不足以共享文件。job 的 Git objects 已独立，不依赖 clone cache 的 alternates。

## 可选 Compose worker

基础镜像包含 Python/Git/API/UI，**不包含 Agent 或平台 CLI 的安装与身份**。最直接的容器选项是 OpenCode Serve；若选本地 CLI，先自行构建带 CLI 的派生镜像并提供该账户认证。

```bash
# conf/.env 中的 OPENCODE_API_URL 在容器内应指向实际 Serve，不能用宿主的 127.0.0.1。
# 例如 http://host.docker.internal:4096，或共享网络中的 Serve service 名。
docker compose --env-file conf/.env up --build -d
docker compose --env-file conf/.env --profile agent up --build -d
docker compose --env-file conf/.env --profile agent run --rm agent-worker python -m biz.agent.worker --check
```

第二个命令同时启动 app 与 worker。它们共用 `data/`（SQLite 与 workspace）和 `log/` bind mounts。`--env-file` 同时用于 Compose interpolation；`CODE_REVIEW_ENV_FILE` 可更换 service env 文件，`CODE_REVIEW_IMAGE` 可选镜像，`CODE_REVIEW_USER=1000:1000` 可选 UID/GID。默认兼容现有 `0:0`，Supervisor 不强制 root。非 root 运行前由操作者创建并设置 bind mount ownership；在 Linux 上使用实际执行账户 UID/GID，在 macOS 上核对 Docker 共享目录权限。

Compose 的 worker 默认 endpoint 为 `http://host.docker.internal:4096`，并配置 host-gateway。**该默认值只解决网络地址**：宿主 Serve 看不到容器的 `/app/data/...`，必须额外映射双方一致的绝对 job 路径。可以将 worker 与独立 Serve 容器的 job 目录均挂载到 `/app/data/agent-worktrees`，或配置宿主与容器相同的 `AGENT_WORKTREE_PARENT` 并添加对应 bind mount。`conf/agent_repos.yml` 如需覆盖也要显式挂载到两个 app/worker 配置路径。不要把宿主 credential 复制到镜像。

## 停止与恢复

SIGTERM/SIGINT 停止新 claim，等待 `AGENT_WORKER_SHUTDOWN_GRACE`（默认 30 秒），再终止活跃 backend。CLI 使用任务 process group 的 TERM/KILL；OpenCode 请求对应 session abort。heartbeat 失败或 lease 丢失触发**当前 job**取消，其他 review 可继续。默认 `AGENT_BACKEND_TIMEOUT=-1`；设置正数可以限时，Git/会话建立/清理仍各有正数 timeout。

Agent 启动前的临时失败可退避重试；启动后不会自动重试。`failed`/`timed_out` 与 `delivery_status` 独立：有效本地原生回执可以在 backend 失败后仍 `confirmed`。此状态不代表平台回查；无效/缺失回执不推进 rolling history。`delivery_error`、`cleanup_error`、backend `error` 分别记录。

当 worker 崩溃且 Agent 已启动，maintenance 将过期 job fence 为 `failed/unconfirmed` 并保留目录。OpenCode abort 未确认也保留目录。恢复顺序：

1. 在 `AGENT_JOB_DB` 对应 SQLite 记录中查看 `idempotency_key`、`backend`、`job_root`、`error`、`cleanup_error`、`delivery_error`、`agent_result`；不要直接把状态改回 queued。OpenCode 可从错误中的 session ID/Serve 记录定位，CLI 从执行账户的进程组定位。
2. 确认对应 CLI 已退出或 Serve session 已停止。abort 请求失败时先处理 Serve，而不是删除其正在使用的文件。SQLite fencing 无法撤回已发出的平台操作。
3. 用已认证的平台 CLI 人工查看目标 review 的唯一 marker、正文两条 revision、已创建的 stacked fix，判断实际交付；保留有用 native receipt/log 证据。不要据纯文本成功声明自动标 confirmed。
4. 外部执行确实结束后，仅删除记录中的 owned job/clone 路径；原始 `repo_roots` checkout 不属于清理对象。以后新的 webhook snapshot 正常进入队列；如果要重复当前 snapshot，由操作者明确决定后再操作队列，程序不提供自动重跑已启动任务的按钮。

DB 终态记录默认保留 90 天；未知执行的保留目录不会被 DB retention 自动删掉，需操作者处理。legacy 内置 LLM/agentic 的 multiprocessing queue 不持久化，本轮没有为它增加 replay ledger；External Agent 使用 SQLite durable queue。

## 本地离线容器验证

```bash
docker build -t ai-codereview-opencode:reliability-check .
docker run --rm --network none --user 1000:1000 \
  --tmpfs /app/log:uid=1000,gid=1000 --tmpfs /app/data:uid=1000,gid=1000 \
  -i ai-codereview-opencode:reliability-check python - < scripts/smoke_worker.py
```

脚本用临时 DB、fake executor 验证 claim/finish 和 Agent-only API import/config check，不消费生产 queue，不调用 provider/model。它证明基础镜像与非 root 本地执行条件，不证明真实模型/CLI/Serve 发布。
