# 受信 Agent 的可靠性改造计划

日期：2026-09-30。状态：**已确认并授权实施；全部 8 个实施阶段已完成（含最终契约修复）**。本轮先收敛项目文档，再直接按阶段实施；不生成 `/tmp` 提示词。各阶段遵循 `implement-in-stages`，检查通过后各自提交；不推送。

## 接手与基线

仓库：`wufei-png/AI-Codereview-Gitlab-Opencode`。本地路径：`/Users/wufei2/github.com/wufei-png/AI-Codereview-Gitlab-Opencode`。

先读根 `AGENTS.md`、[ADR-0007](../adr/0007-trusted-agent-reliability-boundary.md) 和本计划的执行记录，再核对 `git status --short --branch` 与 `git log`。只实施第一项尚未完成且依赖满足的阶段。阶段可以分会话执行；每个完成记录必须包含提交、命令、结果及尚未验证的外部条件。不能把其他会话或其他仓库的测试当作本仓库证据。

来源报告是 `project-portfolio-review-2026/reports/02-AI-Codereview-Gitlab-Opencode.md`，报告基线 `74ad66d2f72888df2aa83e9f0b259937611db39c`。本计划核对基线 `a627b02f9b9bc81608200273ac33e94b1bc3a950`；唯一后续功能修复是 ReadFileTool UTF-8 采样边界。生成计划时 tracked 工作区干净。

本次已运行 `.venv/bin/python -m pytest -q`，149 passed；加 `--cov=biz.agent --cov-report=term-missing` 后仍 149 passed，Agent 总覆盖率约 79%，runner 94%，safety 100%，worker 0%。这些是本地 deterministic 测试，不是 provider/CLI live smoke。

## 核心契约

目标是受控自用/团队内网、主机优先。Agent 继续自主审查、修复、使用已认证 CLI 发布；不增加结构化 findings/result、patch 交接或服务发布模式。保留四个显式 backend、现有平台能力、完整 merge-base 审查和 stacked fix。receipt 只在框架中增加本地验证，不做平台回查。完整取舍记录在 ADR-0007。

Webhook 认证不依赖 Agent/LLM 开关；使用现有 provider secret/signature 原语，缺必要 secret 时 endpoint fail closed。去除完整 payload/response 日志。External Agent enqueue 保持现有 SQLite 原子事务；不增加先写 delivery ledger 再 enqueue 的流程。legacy queue 的重复/崩溃边界保持原有语义，文档明说。

provider HTTP 保持 TLS 验证，支持企业 CA，显式 connect/read timeout。只对安全读取作有界 retry；评论 mutation 不盲目重发。保留平台 facade，不创建全能 provider protocol。

lease 丢失或 heartbeat 失败要取消当前任务；其他 review 不受影响。取消先于正常清理；无法确认远程 abort 时不宣称停止成功。已有 retry-before-Agent 与不 retry-after-Agent 不变。SIGTERM 全局关闭有界。

框架提供 exact detached worktree，保持 Agent 修复分支自由；clone objects 不依赖 job 外共享路径。native receipt 正文使用已有 marker 和 revisions，不增加 Agent 字段；错误独立记录，不把 delivery failure 覆盖成 backend failure。

## 报告待办裁决

| 报告项 | 裁决与落地位置 |
| --- | --- |
| P0-A 全路径鉴权 | 做，阶段 1；replay ledger 暂缓，保留现有 durable revision dedup |
| P0-B TLS/timeout | 做，阶段 2；统一 transport、CA、safe retry、GitHub API base |
| P0-C 服务端 publication/schema/ledger | 不采纳；阶段 5 仅加强现有 receipt 本地校验 |
| P0-D 默认 Docker worker/去 root | 改为阶段 6 主机优先与可选 Compose worker；非 root 可配置 |
| P1-A lease ownership/cancellation | 做实际取消，阶段 3；复用现有 lease token，不扩写 owner 协议 |
| P1-B service-owned worktree | 做，阶段 4；减少 Agent 准备步骤、清理对象依赖 |
| P1-C hardened profile/凭据剥离 | 暂缓；保留可信 Agent 的 CLI、网络和自动修复 |
| P1-D provider capability | 阶段 2 补契约、Enterprise base、Gitea unsupported；不新增 push API |
| P1-E CI/metrics/release | CI 提前到阶段 1，stage 7 增 Docker/release gate；完整 metrics 暂缓 |
| P2 文档/供应链 | 文档随行为同步，阶段 7 收口；SBOM/attestation 暂缓 |
| PUSH_REVIEW_ENABLED/已有 Gitea PR comments | 保留开关和实现，删误导 TODO；不重复开发 |
| Gitea agentic 必然降级 | 报告结论不成立；兼容 payload 已可解析。通过 fixture/fork 契约定位实际缺口 |
| June/August checkbox | June 标历史，August 引用新计划；不据 checkbox 自动追加需求 |

## 分阶段执行

每阶段只依赖前序阶段，交付独立有效结果，最多一项功能提交。文档准备提交、下列 7 阶段及最终验证发现的契约修复合计不超过 10 个阶段。若证据改变计划，只更新未完成项并说明理由；不为凑阶段引入重构或无效测试。

### 1. 基础 CI 与可信入口

依赖：无。修改：`.github/workflows/ci.yml`、`biz/api/routes/webhook.py`、`biz/utils/webhook_security.py`、对应 route tests、配置与 README。

新增 PR/main pytest gate（Python 3.11 起，与 pyproject 最低版本一致）；安装 `requirements.txt`，不能用当前依赖为空的 pyproject/uv.lock 冒充锁定运行环境。覆盖率先报告，不用现有 79% 基线阻塞安全修复。

在 JSON 业务解析、provider API/queue 之前无条件验证 raw body。保留现有 GitLab legacy secret/Standard Webhooks 与显式 access-token fallback。关闭所有 review 时仍认证。平台 API token 使用服务配置，webhook secret 不充当 API token；Agent-only 路径不新增项目 API token 要求。业务日志只记录 provider/event/action/review 摘要，不打印完整 payload。

验收：三 provider × Agent/LLM on/off；有效/无效/缺 secret；篡改 body；malformed JSON；未经认证不会 enqueue；Agent-only 合法事件仍正常入 durable queue。CI YAML 有正确 trigger/权限/依赖安装，测试无真实 API 调用。

检查：`python -m pytest tests/test_webhook_security.py tests/test_external_agent_webhook.py tests/test_webhook_route_security.py -q`；全套 pytest；`git diff --check`。

### 2. Provider HTTP 与真实能力

依赖：1。修改：新 `biz/platforms/http.py`、三平台 handlers、必要的 resolver 修复、`tests/platforms/`、配置/能力说明。

统一 timeout（默认 connect 5s/read 30s）、TLS/CA 与 bounded safe GET/HEAD retry；`Retry-After` 支持秒与 HTTP date，并限制等待。401/403/404/422 不盲目 retry，POST/PATCH/PUT 不自动重发。配置 `PROVIDER_CA_BUNDLE`、`PROVIDER_CONNECT_TIMEOUT`、`PROVIDER_READ_TIMEOUT` 和 `GITHUB_API_URL`；GitHub 默认仍 public API。删除 production `verify=False`。保留分页和已有 facade。错误摘要不带 Authorization 或整段响应。

明确 Gitea push comments unsupported，仍可生成通知/dashboard 结果；不伪装为已发布，不发明 API。补 GitHub/Gitea-shaped PR/push/fork resolver fixtures；只有失败证据才修 resolver，特别核对 head source repo 与 target repo 的区别，不依据函数末尾注释判断。

验收：所有 production provider requests 使用 timeout 和 TLS；custom CA；安全读取 429/5xx bounded retry；mutation timeout 只执行一次；Enterprise 路径；分页；能力测试不发送网络请求。

检查：`python -m pytest tests/platforms tests/agent/test_e2e_webhook.py -q`；全套 pytest；`rg -n 'verify=False|requests\.(get|post|put|patch|delete)' biz/platforms`；`git diff --check`。

### 3. 任务级取消与 worker 生命周期

依赖：2。修改：`backends.py`、`service.py`、`worker.py`、必要的 job store API、worker/lifecycle tests。

框架 backend 接口接受任务级 cancellation event，所有 adapters 保持无交互执行。CLI 对相应 process group TERM→bounded grace→KILL；OpenCode abort 对应 session。保留 global shutdown，不能用它处理单 job lease loss。heartbeat false/异常通知当前任务；lease 检查失败不能被解释成 revision duplicate/confirmed delivery。

保持现有 claim token 与同 review URL 串行，不新增 owner/expiry schema。协调 maintenance 与当前进程的 active job 清理，避免先删正在使用的目录；API 不再独立争抢 worker workspace reaping。crash 后未知外部执行不自动重试，远程 abort 不确定性记录并进入操作说明。

验收：A 丢 lease 会停止 A，B 继续；取消前/期间/完成后的 race；timeout/SIGTERM；OpenCode abort failure 不宣称已停止；Agent 启动后的失败不重跑；worker `--once` 用隔离 DB/fake backend 领取并完成任务，不消费操作者真实 queue。

检查：`python -m pytest tests/agent/test_worker.py tests/agent/test_external_agent_review.py tests/agent/test_durable_agent_review.py -q`；全套 pytest；`git diff --check`。

### 4. 服务准备 exact worktree

依赖：3。修改：`workspace.py`、`review_request.py`、`backends.py`、canonical skill、worktree/prompt tests、ADR-0001/0002/README。

job 内独立 objects 替代 `.agent-source` 的 `clone --shared`，服务创建 `worktree/`，HEAD 等于 resolved Source Revision，fork target revision 同样可在目录内解析。三种本地 CLI cwd 指向 worktree；OpenCode 保留 job 根作为会话目录加载框架 config，任务 context 明确提供 worktree。config/skill/receipt 位于拥有的 job 目录，共享 job 路径即可工作。此调整避免生成配置覆盖 source worktree 的文件。处理 native sandbox 对 receipt 写路径的要求，但不把它升级成权限剥离工程。

Prompt/skill 给现成 `WORKTREE_PATH`，不再要求 Agent worktree add/clone/fetch；可减少 workspace 别名，保持 revisions、previous note 和 stacked fix 上下文。Agent 可以创建自己的 fix branch。清理使用 Git worktree remove/prune，失败独立记录；不改原始 checkout。取消未停止时不删除执行目录。

验收：local/clone/fork，exact detached HEAD、target object、无外部 alternates；原始 checkout/remote 无变化；创建 fix branch 后可正常 remove；成功/失败/取消 cleanup；四 backend cwd/config 接口与现有 native result 不变。

检查：`python -m pytest tests/agent/test_workspace.py tests/agent/test_external_agent_review.py tests/agent/test_durable_agent_review.py -q`；全套 pytest；`git diff --check`。

### 5. 原生 receipt 与快照绑定

依赖：4。修改：`service.py`、内部 receipt validator、必要的 job store 字段、receipt tests、ADR-0004/0006。

本地读取现有 receipt，不重写 JSON、不新增 model-generated result。合法 note ID、provider body/note 中唯一 exact hidden marker 和两条 authoritative revision 均匹配；native 字段中的目标 URL/标识若能核对就必须一致，不把 GitLab noteable_id 错当 iid。缺字段或矛盾保持 unconfirmed，原始 receipt 保留用于诊断，delivery error 与 backend/cleanup error 分开。native API URL 与浏览器 review URL 要按 provider 语义比较，不能仅比较 host。

历史 confirmed 记录不批量重分类；新执行以新验证器为准。backend failed + 完整有效 receipt 仍可 confirmed；仅 `{id:9}` 不行。所有 receipt 校验仍在 cleanup 前完成；lost lease 不更新其他 owner 的记录。confirmed 表达可信 Agent 的 snapshot-matching report，明确未做 provider readback。

验收：三 provider 原生 fixture/extra fields 原样保存；缺正文、空/非法 ID、错误 marker、重复 marker、另一 source/target、另一 review URL；后台失败但已交付；未交付不推进 previous history；不为校验失败重跑 Agent。

检查：`python -m pytest tests/agent/test_delivery_receipt.py tests/agent/test_durable_agent_review.py -q`；全套 pytest；`git diff --check`。

### 6. 主机运行与可选 Compose worker

依赖：5。修改：`worker.py`、配置检查/依赖、Dockerfile、Compose/Supervisor、operations 文档和启动契约测试。

worker 在导入依赖前按与 API 相同的方式加载项目 `.env`，不覆盖已导出的变量。Agent-only API 启动不探测未启用的内置 LLM。提供不领取任务的 `--check` 配置/目录/backend 基础检查；OpenCode 不要求 worker 本机装平台 CLI。认证与 CLI 安装仍由操作者负责。

主机说明包括 cwd、同一账户的 CLI 身份、共享 job 绝对路径、启动和 SIGTERM。Compose 使用可选 profile/worker service、共用 DB 与 runtime volumes、显式 backend endpoint，不把容器 localhost 当宿主/其他容器。镜像与 Python minimum 对齐，移除旧固定上游镜像名；可配置非 root UID/GID，记录 bind mount ownership。不要复制宿主 credentials 进镜像或给缺 CLI 的基础镜像虚构支持。

验收：环境覆盖顺序；无 LLM 配置的 Agent-only 启动；`--check` 不领取 job；临时 DB 的 worker smoke；Compose 配置和有工具时 build/runtime smoke；缺 Docker daemon/真实 backend 单独记录，不能说已通过 live smoke。

检查：`python -m pytest tests/agent/test_worker.py tests/test_startup.py tests/test_deployment.py -q`；全套 pytest；`docker compose config`（使用临时非私密 env fixture）；可用时 docker build/smoke；`git diff --check`。

### 7. 发布门与文档收口

依赖：6。修改：CI/release workflows、README、`.env.dist`、June/August 历史文档、operations runbook、provider matrix、本执行记录。

PR/main gate 覆盖新契约；添加 Docker build/isolated worker smoke。release tag publication 依赖同一 SHA 的测试/build gate，不仅依赖 tag 格式。Actions 固定可核实的 commit SHA；不新增 SBOM/attestation。本轮 coverage 以行为验证为主，报告 Agent/runner/safety 数值和与目标的差距，不为了覆盖率测试实现细节。

June 文档明确 historical/superseded；August 文档引用新 ADR，不按旧 checkbox 开发。同步四 backend、timeout 默认值、webhook secret 全路径要求、legacy process queue 与 SQLite 区别、Gitea capability、部署选项、receipt confirmed 边界及未知远程执行的人工恢复步骤。只创建实际有内容的文档，不为目录清单凑占位文件。

验收：全套和覆盖率；stage 契约完整；release gate 的同 SHA 依赖；文档与配置/命令一致；无 secrets、private inputs、runtime output 被提交。

检查：`python -m pytest -q --cov=biz.agent --cov-report=term-missing`；deployment checks；`git diff --check`；每个 staged diff 的范围审查。

### 8. 最终契约修复：Enterprise receipt host

依赖：7。最终范围核对新增 fixture 后发现：Enterprise review 在未设置 `GITHUB_API_URL` 时错误接受公网 `api.github.com` 的 native issue URL。旧实现新增测试 1 failed/1 passed，证据改变了“所有可识别目标信息已一致”的结论，因此增加一个独立修复阶段，准备提交 + 8 阶段仍小于 10。

仅调整 `delivery_receipt.py` 的允许 API 主机：公网 `github.com` review 才自动允许 `api.github.com`；企业 review 默认用自身主机，显式 `GITHUB_API_URL` 仍可指定不同的 API gateway。没有新增 Agent 字段、provider readback 或权限策略。检查 receipt/durable regression、全套 coverage、重建镜像并重跑离线 smoke。

## 验证与交付边界

使用本仓库 `.venv/bin/python`；无环境时按 AGENTS 安装 requirements，不借用其他仓库环境。新增测试用 fake HTTP/CLI/OpenCode 和 tmp_path，关键 race 用事件同步，不靠长 sleep。每阶段 stage explicit paths、检查 staged diff 和 `git diff --cached --check`，检查通过才提交。不要提交 `conf/.env`、runtime DB/log、.coverage 或用户无关改动；不推送。

本地/CI 证明入口、HTTP contract、queue/cancel、worktree、receipt 与配置行为。真实 GitHub/GitLab/Gitea 评论和四种 CLI/Serve 执行必须另行使用已授权测试仓库与身份，不从现有生产 queue 做 smoke；权限或外部条件缺失时报告未验证，不能凭组装命令声称已证明 native isolation、model transport 或真实发布。

## 执行记录

| 阶段 | 状态 | 提交/检查/限制 |
| --- | --- | --- |
| 计划与 ADR | 已写入 | `272b6ee`；用户确认设计并授权实施；基线与 149 项测试已核对 |
| 1 入口与 CI | 已完成 | `0e9858b`；route/security 42 passed；全套 183 passed；CI 已配置，GitHub 执行待推送后验证 |
| 2 HTTP/capability | 已完成 | `ac88fb5`；provider/resolver 36 passed；全套 216 passed；三平台 fork source 解析修复；无 live provider smoke |
| 3 取消/lifecycle | 已完成 | `6bcb498`；worker 11 passed；全套 227 passed；任务级取消与 native stdout 验证；未知远程执行保留目录人工检查 |
| 4 worktree | 已完成 | `a11c966`；workspace 6 passed；全套 233 passed；含借用 alternates 的 local seed 与 fork；CLI cwd 固定 SHA，OpenCode job 根加载配置；无 live backend smoke |
| 5 receipt | 已完成 | `a53adba`；receipt/durable 41 passed；全套 264 passed；三平台 native 回执、本地快照匹配与 failed+confirmed；原文和独立 delivery_error 入库；未做 provider readback |
| 6 operations | 已完成 | `4ce9b9b`；worker/startup/deployment 17 passed；全套 270 passed；Docker arm64 build、断网非 root fake-worker/API smoke、Supervisor API/UI health 均通过；未消费真实 queue/调用模型 |
| 7 release/docs | 已完成 | `aae4f72`；Python 3.11/3.12 各 270 passed；Linux image 269 passed/1 skipped（无 Docker CLI）；coverage 85%、runner 94%、safety 100%；Actions refs 已查上游；workflow lint 通过；GitHub 执行/实际发布尚未发生 |
| 8 最终契约修复 | 已完成 | 提交主题 `fix(agent): bind native API receipts to the configured platform`；新增 regression 先 1 failed/1 passed，修复后 receipt/durable 43 passed；最终 Python 3.11/3.12 各 272 passed；重建镜像与离线 smoke 通过；Linux 271 passed/1 skipped |


阶段 7 验证：`.venv/bin/python -m pytest -q --cov=biz.agent --cov-report=term-missing`，270 passed，Agent 85%（目标 80%）、runner 94%（目标 90%）、safety 100%（目标 100%），均达原有目标；worker 从基线 0% 到 87%。Python 3.12 使用 `/tmp` 中本仓库专用临时 venv，按 requirements 新安装后 270 passed，环境已移除。没有使用其他项目的 venv/lock 作为证据。

Docker arm64 使用当前 Dockerfile 构建成功；非 root `1000:1000`、断网、tmpfs/临时 DB 的 fake-worker/API smoke 与 Supervisor API/UI health 通过。基础镜像中挂载 tests/pytest.ini 后断网全套为 269 passed、1 skipped：Compose test 缺容器内 Docker CLI，其宿主测试已通过。CI 将在 Ubuntu 上检查 Python 3.11/3.12 与 Docker（amd64），真实 GitHub runner 和多架构 registry publication 尚未执行，不能把本地 arm64 build 当作远端发布结果。

所有外部 Action SHA 于 2026-09-30 用官方仓库 `git ls-remote` 核对对应 v4/v5/v3/v6 tags。workflow 静态检查使用 `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.10 .github/workflows/ci.yml .github/workflows/build_images.yml`；v1.7.12 要求 Go 1.25，本机 Go 1.24，因此使用仍支持当前语法的 v1.7.10。

最终验证（阶段 8 后）：Python 3.11 coverage 全套 272 passed，Agent 85%、runner 94%、safety 100%、worker 87%；重新创建本仓库专用 Python 3.12 临时 venv 安装 requirements 后 272 passed，环境已移除。重建 Docker arm64 镜像成功，断网非 root smoke 通过；Linux 全套 271 passed/1 skipped，唯一 skip 仍是缺容器内 Docker CLI 的 Compose 检查。workflow lint 与整体 `git diff --check` 通过。文档准备 + 8 个实施阶段共 9 个本地提交，不推送。

阶段 8 自引用提交以主题记录；可执行 `git log -1 --format=%h --grep='^fix(agent): bind native API receipts to the configured platform$'` 查询。外部条件限制仍是实际 backend/provider 发布、远端 GitHub workflow 与 registry publication 未验证；这些未被离线 smoke 或本地构建替代。
