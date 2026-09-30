# Trusted Agent Reliability Boundary

**Status: accepted, 2026-09-30.** 用户已确认设计并授权分阶段实施。部署目标是受控自用或团队内网，主机运行优先，Compose 提供可选 worker。Agent 是被信任的执行者。

## 决策

Agent 继续负责完整审查、自然语言报告、自动修复和已认证平台 CLI 交付。保留 native text/JSON/JSONL Agent Result 和 provider-native delivery receipt，不引入 ReviewResultV1、结构化 findings、patch 交接协议、service-owned publisher 或 publication ledger。自动修复继续按 ADR-0005 创建面向原始 source branch 的 stacked PR/MR。

框架负责入口认证、安全且有界的 HTTP、durable queue、任务取消、工作目录准备、清理和结果记录。框架可以增加内部结构和验证，但不能将这些结构变成 Agent 必须填写的新输入输出协议。

框架在执行前创建 exact Source Revision 的 detached worktree，Agent 直接使用提供的工作目录并自行选择修复分支。所有需要的 Git objects 和运行资料位于 job 目录内，避免 OpenCode 共享目录之外的 Git alternates 依赖。框架不修改操作者的原始 checkout。

交付确认采用本地校验现有 receipt：检查合法 note ID、正文中唯一的现有 hidden marker、Source/Target Revision，以及 provider-native 字段中能够核对的目标信息。缺失或冲突保持 unconfirmed。原生 JSON 原样保存，额外 provider 字段保留；backend execution 和 delivery 仍独立，失败 backend 也可能已经有效交付。这里的 confirmed 是受信 Agent 的匹配交付报告，不是平台回查或对恶意 Agent 的证明。

保持已有 lease token fencing、同 review URL 串行、两层 revision 去重和 Agent 启动后的不自动重试。丢 lease 时取消对应任务的 backend；全局退出仍终止所有 active backends。取消不能撤回已经完成的网络副作用，未知远程执行状态需要操作者检查，不宣称严格 exactly-once publication。

保留网络、CLI 配置和凭据能力，以及 unlimited backend timeout 默认值。专用账户、非 root 和有限 timeout 是部署选项，不强制把本项目改成多租户沙箱。认证对所有 webhook review 路径生效；生产路径不关闭 TLS，企业 CA 显式配置。

## 不采纳或暂缓的报告建议

- 不创建独立 webhook replay ledger：External Agent 已有持久 revision 去重；本轮不迁移 legacy multiprocessing queue，也不承诺 legacy delivery-ID 去重。避免 ledger 与 enqueue 分开提交带来的丢任务窗口。
- 不增加 lease owner/expiry 协议或替换 SQLite；已有 token 和更新时间够用，SQLite connection 已有 30 秒锁等待。
- 不剥离 Agent 平台 write token、不默认禁网或 bypass/native CLI 能力，不新增 hardened profile。
- 不做 strict current-head/target publication fence、自动 target-advance 重跑或完整发布恢复状态机。报告标明实际审查快照，后续 revision 由现有串行队列处理。
- 不补 ast_query，不按历史 checkbox 重做已经实现的 backend、queue、fork revision 或 Gitea PR comments。
- 不新增 Gitea push comment API；明确现有 unsupported 行为。Gitea-compatible payload 已可通过 shared GitHub-shaped resolver，补契约验证后只修有证据的缺口。
- 完整 metrics/trace、SBOM、attestation、多主机 queue 和服务自动管理 CLI 登录不在本轮范围。

## 与既有 ADR 的关系

保留 ADR-0001/0002 的 Agent-owned delivery、原生输出与操作者认证边界；仅调整 worktree 的创建者和默认执行 cwd。保留 ADR-0003 的队列语义、ADR-0004 的完整审查/增量报告、ADR-0005 的 stacked fix，以及 ADR-0006 的 provider-native receipt。ADR-0006 的 confirmed 条件加强为本地 snapshot 校验，仍不要求新 JSON 协议。

实施范围、顺序、验证及执行记录见 [实现计划](../plans/2026-09-30-reliability-implementation-plan.md)。
