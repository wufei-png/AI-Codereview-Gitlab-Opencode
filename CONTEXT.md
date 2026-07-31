# Agent Review Context

本项目把 webhook 触发的代码审查视为一个可追踪的 Review Job，并允许由不同 Agent Backend 执行。

## Review execution

**Agent Backend**:
执行一次 Review Job 的 Agent 运行时。OpenCode Serve、Codex CLI 和 Claude CLI 是并列的后端。
_Avoid_: LLM provider, model

**Review Job**:
针对一个代码托管平台项目及其一次变更触发的审查任务；任务开始时 fetch source branch 的最新 revision，任务可以包含审查、自动修复和结果提交。
_Avoid_: request, session

**Agent-owned delivery**:
由 Agent 直接通过 GitLab/GitHub/Gitea 的工具或 CLI 提交审查结果及可选修复 MR，主服务负责派发任务和提供运行上下文。
_Avoid_: centralized delivery

**Auto-fix**:
当 Agent 判断修复明确且安全时，直接修改代码并创建修复 MR 的默认行为。
_Avoid_: silent fix

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

**Platform CLI Delivery**:
Agent 使用目标代码托管平台的已认证 CLI 提交 review note 和可选修复 MR；本项目不负责 CLI 的登录、token 配置或认证生命周期，也不使用 MCP。
_Avoid_: MCP delivery, service-managed auth
