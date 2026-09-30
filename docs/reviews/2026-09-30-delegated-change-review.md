# 2026-09-30 整体实现审查

## 范围与方法

按 delegated-change-review 执行一次独立审查。一个全新上下文、只读的 review-agent 审查了 `a627b02f9b9bc81608200273ac33e94b1bc3a950..324baef12daf110fe742ce1d7f0df064bcbb4062` 的全部 9 个提交、52 个变更路径；该范围覆盖计划、全部实施阶段及最终 Enterprise receipt 修复。审查目标、依赖顺序与验收契约来自 ADR-0007 和 reliability implementation plan。

保留已确认的设计：可信内部 Agent 自行审查、修复和发布，框架管理入口、HTTP、queue、取消、worktree 与原生 receipt。原生输入输出不增加 findings/result schema；不把 provider readback、权限剥离、hardened profile 或自动重跑未知执行补成隐含需求。主代理逐项独立复现并裁决，审查者没有修改文件或提交。

## 接受并修复的发现

**P2：历史 SHA 会掩盖当前回执的 revision 矛盾。** 审查目标的 `biz/agent/delivery_receipt.py:106-108` 只检查 source/target SHA 是否在正文任意位置出现。请求 source=A、target=B 时，正文写当前 Source Revision=C、Target Revision=B，再在历史中提到 A，仍会确认；Source/Target 值写反也会确认。错误的 confirmed 记录会推进 previous reviewed revision，后续去重可能跳过未实际交付对应快照的审查。

主代理用隔离临时 DB、fake backend 和 workspace 独立复现：上述矛盾回执被保存为 `completed/confirmed`，历史推进到 A。新增回归测试在修复前得到 **5 failed / 7 passed**，包含四类矛盾及任务历史集成场景。

修复仅增加框架侧对已有当前 Source Revision/Target Revision 标签的可选识别：纯文本、常见 Markdown、表格、下划线标签以及 Current 前缀中的完整 SHA 若与对应快照矛盾，保持 `unconfirmed`。历史中的正确 SHA 不再掩盖该矛盾；重复的当前标签也不能包含冲突值。`Previous Source Revision` 不作为当前字段。未使用这些标签的原生正文继续沿用完整 SHA、marker 与 native target 校验，不要求 Agent 改用固定模板或添加字段。

原始 JSON/正文仍原样保存，`delivery_error` 与 backend error 分开；校验失败不推进历史，也不重跑已经启动的 Agent。修复提交主题为 `fix(agent): reject contradictory receipt revision metadata`，可按主题查询提交。

## 排除项与限制

审查中另有候选场景：忽略 SIGTERM 的进程组后代可能存活。对比基线后发现相关 `_terminate_process_groups` 实现早已存在，主代理也确认其 AST 与基线完全一致；该候选未作为本次引入的 finding，不扩大本次修复范围。

本轮最终报告只有上述一项 P2，没有要求撤销可信 Agent 架构或增加结构化输出。修复后由主代理完成验证，没有再启动独立审查轮次，因此不宣称得到第二轮 `No findings`。

## 验证

- 审查开始时在目标提交重新执行全套 coverage：272 passed。
- 修复后执行 `.venv/bin/python -m pytest tests/agent/test_delivery_receipt.py tests/agent/test_durable_agent_review.py -q`：57 passed，包含写反、历史掩盖、重复当前字段、Markdown/table 标签、灵活原生正文及不推进历史/不重跑集成场景。
- 修复后执行 `.venv/bin/python -m pytest -q --cov=biz.agent --cov-report=term-missing`：Python 3.11，286 passed；Agent 85%、runner 94%、safety 100%、worker 87%，receipt validator 96%。
- `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.10 .github/workflows/ci.yml .github/workflows/build_images.yml` 通过；整体及 staged diff whitespace 检查通过。

本轮未调用真实模型/backend、未向 provider 发布评论、未消费真实 queue；未重新执行 Python 3.12 或 Docker build/runtime。此前阶段的这些检查记录保留在实施计划中，不当作修复后的新证据。远端 GitHub workflow 与 registry publication 仍未在本轮核验。本轮修复仅作本地提交，不推送。
