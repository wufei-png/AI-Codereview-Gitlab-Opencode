# Provider 能力与 HTTP

| 能力 | GitLab | GitHub | Gitea |
| --- | --- | --- | --- |
| 内置 MR/PR 评论 | 支持 | 支持 | 支持 |
| 内置 push commit 评论 | 支持 | 支持 | unsupported；通知/dashboard 仍可用 |
| External Agent PR/MR 审查/修复/评论 | 由已认证 CLI 执行 | 由已认证 CLI 执行 | 由配置的已认证 CLI 执行 |
| API base | GITLAB_URL + api/v4 | GITHUB_API_URL，默认 api.github.com | GITEA_URL + api/v1 |

以上是代码契约，不是具体平台版本或 CLI 的 live smoke 证明。三平台内置 agentic 的 fork repo 解析使用 source/head 项目；GitHub-shaped Gitea payload 并非必然降级。未知 payload 仍按现有策略降级为 diff_only。

平台 REST transport 显式校验 TLS，默认 connect/read timeout 为 5/30 秒。企业证书使用 `PROVIDER_CA_BUNDLE`，timeout 使用 `PROVIDER_CONNECT_TIMEOUT`/`PROVIDER_READ_TIMEOUT` 正数配置。GET/HEAD 对 429/502/503/504 和临时连接/超时最多尝试三次，Retry-After 等待最多 60 秒。证书错误、401/403/404/422 不自动重试；所有 mutation 只发送一次，不自动重发可能已成功的评论。

External Agent 使用平台 CLI 的网络策略，框架 HTTP 设置不替代 CLI 配置。平台认证仍由操作者维护；本轮不实现 provider 回查、服务端发布或 Gitea push comment API。
