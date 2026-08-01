# Agent Review Context

本项目把 webhook 触发的代码审查视为一个可追踪的 Review Job，并允许由不同 Agent Backend 执行。

## Review execution

**Agent Backend**:
执行一次 Review Job 的 Agent 运行时。OpenCode Serve、Codex CLI、Claude CLI 和 Pi CLI 是并列的后端。
_Avoid_: LLM provider, model

**Review Job**:
针对一个代码托管平台项目及其一次变更触发的审查任务；任务开始时解析 Source Revision 和 Target Revision，任务可以包含审查、自动修复和结果提交。
_Avoid_: request, session

**Review Job Record**:
由服务维护的 Review Job 稳定事实，包括 backend、Source Revision、Target Revision、Previous Reviewed Source Revision、Previous Review Note ID、时间、Execution Status、Delivery Status、错误和清理错误；清理失败只写入 `cleanup_error`，不覆盖由 backend 执行决定的状态。它不推断 Agent 是否忠实完成了文本中的每一步。
_Avoid_: agent transcript, normalized agent response

**Execution Status**:
Review Job 的服务侧生命周期状态，只使用 `queued`、`running`、`completed`、`failed` 和 `timed_out`。
_Avoid_: agent verdict, delivery status

**Delivery Status**:
Rolling Review Note 的服务侧确认状态，只使用 `not_attempted`、`confirmed` 和 `unconfirmed`。只有有效的平台原生 delivery receipt，或 Delivery Reconciliation 证明恰好一个当前 automation-owned note 并生成 provider-native receipt，才能得到 `confirmed`，即使 Agent Backend 随后失败或超时也一样；它不改变 Execution Status。
_Avoid_: execution status, inferred delivery success

**Delivery Reconciliation**:
当平台 CLI 的发布命令没有返回可解析的原生 JSON 时，Agent 按 Review Note 的确定性隐藏 marker 查询目标 review 的 notes，并要求结果恰好唯一且属于当前快照；将匹配到的平台对象作为 receipt 证据。零匹配、多匹配、缺少 note ID 或无法确认当前快照时保持 `unconfirmed`，不能用“评论成功”等纯文本推断已确认。
_Avoid_: synthetic success receipt, ambiguous marker match

**Backend Timeout**:
只约束 Agent Backend 执行阶段的墙钟时限；默认 `-1` 表示不限制，只有显式配置正数时才启用。Git 准备、OpenCode 会话建立和清理使用各自独立的正数时限。
_Avoid_: review-job deadline, queue timeout

**Agent Result**:
Agent Backend 返回到 stdout 的原生文本、JSON 或 JSONL；服务无论 backend 成功或失败都保存已经产生的 stdout，但不要求不同 Agent 生成统一的模型输出结构。失败时脱敏后的 stderr 写入 `error`。默认不限制保存大小；部署方显式配置上限后允许截断，并由服务记录截断事实。
_Avoid_: normalized result, guaranteed delivery receipt

**Source Revision**:
Review Job 开始时从源项目的 source branch 获取的最新提交，是本次审查和可选修复的 head。
_Avoid_: webhook revision, event SHA

**Target Revision**:
Review Job 开始时从目标项目的 target branch 获取的最新提交，是计算审查 merge base 的 base 端；fork 请求不能用源项目中的同名分支代替。
_Avoid_: target branch name, fork-local base

**Previous Reviewed Source Revision**:
同一 merge/pull request 最新一次确认成功创建或更新 Rolling Review Note 时的 Source Revision。它只用于识别本轮 source delta 和控制重复输出，不能替代 Target Revision 参与完整正确性审查；backend 成功但没有 delivery receipt 时不推进，历史上曾确认过但已被较新快照替代的 revision 也不能触发去重。
_Avoid_: review base, target revision

**Rolling Review Note**:
每个 merge/pull request 由 Agent 创建并持续更新的一条 automation-owned 审查 note。它是当前状态快照，包含当前 revision、本轮新增或变化、仍未解决和已修复的 finding，而不是无限追加的历史记录。finding 使用 `F001` 一类稳定键辅助跨 revision 状态匹配。note 携带确定性隐藏标记；服务保存平台原生 delivery receipt 并提取 note ID，必要时由 Agent 按标记恢复或执行 Delivery Reconciliation。人工编辑会在下一轮被完整覆盖。
_Avoid_: review history log, one note per revision

**Agent-owned delivery**:
由 Agent 直接通过 GitLab/GitHub/Gitea 的工具或 CLI 提交审查结果及可选修复 MR，主服务负责派发任务和提供运行上下文。
_Avoid_: centralized delivery

**Auto-fix**:
当 Agent 判断修复明确且安全时，直接修改代码并创建 Stacked Fix MR 的默认行为。
_Avoid_: silent fix

**Stacked Fix MR**:
Auto-fix 创建的、基于当前 Source Revision、目标为原始 Source Project/Source Branch 的修复变更；合并后推进原始 merge/pull request 的 source branch。fork 请求也保持 source project 作为修复目标，不把修复变成指向原始 Target Project/Target Branch 的独立替代 MR。
_Avoid_: standalone replacement MR, target-branch fix

## Repository workspace

**Source Repository**:
由 webhook 中的远程项目 URL 标识的真实代码仓库。
_Avoid_: target repo

**Review Worktree**:
从已识别的 Source Repository 创建、仅供一次 Review Job 使用的临时 Git worktree；任务结束后必须删除。
_Avoid_: checkout branch, working copy

**Worktree Parent**:
由系统配置的独立目录，Agent 必须在其下创建 Review Worktree；子目录命名、分支命名和 Git 细节由 Agent 自行决定。
_Avoid_: fixed worktree name, fixed branch name

**Clone Workspace**:
当本地没有匹配 Source Repository 时，用于保存临时 clone 的可配置父目录；clone 是否在任务结束后删除由配置决定。
_Avoid_: repo cache

**Repository Mapping**:
由操作者维护的远程 URL 前缀到本地目录根路径的映射。key 可以指向项目组或完整项目；先按最长路径边界匹配并尝试推导的本地目录，再在对应 local root 内按 Discovery Depth 递归查找，并且只有远程 origin 一致的 Git 目录才算 Source Repository。
_Avoid_: guessed repo path, remote alias

**Discovery Depth**:
Repository Mapping 允许向下递归搜索的最大目录层级，用于限制本地仓库发现范围和成本。
_Avoid_: unbounded scan

**Shared Review Skill**:
所有 Agent Backend 共同遵循的一份 canonical、版本化审查指令，同时也是后端提示词。Review Job 通过明确的文件路径要求当前 Agent 读取并执行它；后端差异只存在于调用信封，不复制审查规则。
_Avoid_: backend prompt copy

**Unattended Execution**:
Review Job 执行时没有人处理权限提示；每个 Agent Backend 只获得显式启用的工具，但已启用工具在其实际执行边界内无需逐次人工批准。
_Avoid_: interactive approval, permission popup

**Platform CLI Delivery**:
Agent 使用其执行环境中由操作者预先安装、认证和授权的系统 CLI 提交 review note 和可选修复 MR；本项目只在 Shared Review Skill 中提供可执行的示例命令。本地 CLI Backend 启动前检查本地 Agent CLI 和平台 CLI 是否存在；远端 OpenCode Backend 不能用 worker 本地文件系统推断远端 CLI 能力。服务不探测认证或权限，不签发、不降权、不代理也不验证 CLI 权限，并且不使用 MCP。
_Avoid_: MCP delivery, service-managed auth, service-managed CLI permissions
