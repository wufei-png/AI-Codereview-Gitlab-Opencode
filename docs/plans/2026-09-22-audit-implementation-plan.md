# ReadFileTool UTF-8 边界修复：实现计划

日期：2026-09-22。状态：已实施并通过本仓库验证。

## 目标与接手方式

修复文件读取工具在 8192 字节采样边界拆开 UTF-8 字符的问题，保证合法文本完整进入工具输出。保留现有二进制探测、路径限制、分页和返回格式。

仓库身份为 `wufei-png/AI-Codereview-Gitlab-Opencode`，本地目录为 `AI-Codereview-Gitlab-Opencode`，核对基线为 `74ad66d2f72888df2aa83e9f0b259937611db39c`，计划生成前工作区干净。

先读根 `AGENTS.md`，核对 `git remote -v`、`git rev-parse HEAD`、`git status --short`，再读目标文件、测试与工具注册入口。本计划可独立执行，不依赖另一个仓库先合并；同类修复需在各自代码基线上验证，不能直接宣称共用测试结果。

新会话启动指令：

> 阅读 AGENTS.md 和 docs/plans/2026-09-22-audit-implementation-plan.md，按已确认方案修复 ReadFileTool 的 UTF-8 采样边界缺陷，添加失败先行的回归测试，执行本仓库验证并记录结果。不要扩展到其他行为或其他仓库。

原计划会话只交付计划；本次接手会话已收到实施指令。提交与推送按本次会话授权处理。

## 证据与裁决

来源为 2026-09-22 审计 B01。目标 `biz/agent/tools/read_file.py`，核对时内容 blob SHA 为 `c4a57c9d4834b85728d0410be5b4d0693c773815`。

当前实现先读取 8192 字节 `sample`，单独 `decode(errors="replace")`，再以文本模式重开文件并 `seek(len(sample))` 读取剩余部分。采样点不保证位于 UTF-8 字符边界；两段分别解码会引入替换字符。

本次直接调用当前项目的 `ReadFileTool.execute()`，用临时文件核对了以下输入：

| 输入 | 当前结果 |
| --- | --- |
| `"a" * 9000 + "\n"` | 输出正文与预期一致 |
| `"x" + "é" * 4500 + "\n"` | 不一致，出现两个 U+FFFD |
| `"中" * 3000 + "\n"` | 不一致，出现两个 U+FFFD |
| `"x" + "😀" * 2250 + "\n"` | 不一致，出现两个 U+FFFD |
| 空文件 | 当前空正文正常 |

现有 `tests/agent/tools/test_read_file.py` 的 8 项测试全部通过，但没有上述跨界覆盖。因此现有测试通过不能否认缺陷；以上也不是外部 Agent、webhook 或 provider E2E 证据。

采用最小修复：sample 仅用于现有二进制判别，实际文本从字节 0 使用一个完整 UTF-8 解码流读取。无需引入增量解码器、依赖或缓存。保留 `errors="replace"` 处理真正非法 UTF-8 的既有策略。

## 实施步骤

1. 阅读 `biz/agent/tools/read_file.py`、`biz/agent/safety.py`、`biz/agent/tool.py`、`biz/agent/tools/__init__.py` 和 `tests/agent/tools/test_read_file.py`，确认当前调用链。
2. 在现有测试文件中添加临时文件回归：用 `write_bytes()` 控制字节，调用真实 `ReadFileTool.execute()`；先证明 2/3/4 字节跨界用例在旧实现失败。
3. 保留 sample 探测；删去 sample 的独立解码、文本 seek 和两段拼接，改为从头完整读取文本。用短注释说明采样边界不是字符边界。
4. 补齐完整输出与分页验证：断言 `ToolResult.success`、`error` 和完整 `output`，不能只断言“不含替换字符”或包含一个片段。
5. 执行相关工具、安全和 Agent 测试，再运行仓库全套；核查工具注册后的确定性调用可以返回同一正确正文。复用已存在的 registry 测试入口，不为本修复请求真实 LLM。
6. 更新本计划执行记录，报告修复与测试证据。行为修复和回归测试属于同一交付批次，不混入无关注释清理。

## 回归矩阵与兼容边界

- 2/3/4 字节字符横跨第 8192 字节边界；同时覆盖合法字符恰好在边界结束的对照。
- ASCII、空文件、末尾有/无换行、LF 和 CRLF；输出按现有 `splitlines()` 与换行拼接契约检查。CRLF 本身跨过采样边界时也应正确计行。
- 在跨界长行前后布置额外行，分别验证默认读取与 `offset` / `limit` 切片，核对完整正文和 `lines N-M:`。
- 现有空文件标题 `lines 1-0:` 保持不变；不顺带重定义空文件、越界 offset 或参数归一化行为。
- 真正非法 UTF-8 的替换策略不变；NUL 比例探测、敏感文件拒绝、仓库外路径拒绝与缺失文件错误继续通过。
- 保留当前读完整文件再分页的策略，不把本次修复扩展为大文件性能重构、竞态修复或新的访问授权设计。

## 验证命令

使用本仓库的 Python 虚拟环境。若环境不存在，按 `AGENTS.md` 建立并安装 `requirements.txt`；不要借用其他仓库的环境假装独立验证。

```bash
.venv/bin/python -m pytest tests/agent/tools/test_read_file.py -q
.venv/bin/python -m pytest tests/agent/test_safety.py tests/agent/test_tool_registry.py -q
.venv/bin/python -m pytest tests/agent/ -q
.venv/bin/python -m pytest
git diff --check
```

首次新增测试失败、修复后通过须分别记录。若更广套件存在前置条件或已有失败，分开说明；不将其静默跳过或扩展修复范围。确认只有目标实现、相关测试和本计划执行记录发生预期变化。

## 执行记录

- [x] 回归测试在旧实现上观察到预期失败：新增测试运行 17 项，3 项跨越采样边界的 2/3/4 字节字符用例失败，其余 14 项通过；失败输出分别出现 2/3/4 个 U+FFFD。
- [x] 单流解码修复完成：保留 8192 字节二进制采样及原 NUL 阈值，从文件开头以一次 UTF-8 文本读取生成正文，保留 `errors="replace"`。
- [x] 相关测试和工具注册调用通过：`test_read_file.py` 17 项通过；新增默认工具注册与 `ToolRegistry.dispatch()` 回归后，`test_safety.py` 与 registry 测试 27 项通过，完整 Agent 套件 141 项通过。
- [x] 全套结果、缺失条件及范围审查已记录：`.venv/bin/python -m pytest` 149 项通过；本仓库虚拟环境及依赖可用，无缺失前置条件。测试使用临时文件和确定性注册调用，未执行真实 LLM、webhook 或 provider E2E。

范围审查：实现改动仅涉及 `biz/agent/tools/read_file.py`，回归覆盖在 `tests/agent/tools/test_read_file.py` 与 `tests/agent/test_tool_registry.py`，本文件记录执行证据。未发现未解决事项。
