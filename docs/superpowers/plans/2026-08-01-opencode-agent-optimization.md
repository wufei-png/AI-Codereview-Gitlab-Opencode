# OpenCode Agent and Multi-CLI Review Optimization

## Decision summary

The project will retain OpenCode Serve and add explicitly selected Codex CLI, Claude CLI, and Pi CLI backends. The first phase will not perform automatic CLI routing or fan-out. The Agent remains responsible for posting review notes and creating an optional fix MR through an already-authenticated platform CLI; MCP is out of scope.

The project will maintain one canonical prompt/skill at `skills/review-agent/SKILL.md`. OpenCode, Codex, Claude, and Pi invocation wrappers only provide the task context and the resolved skill path; they must not duplicate review policy.

## Configuration

Add `conf/agent_repos.yml` with defaults equivalent to:

```yaml
repo_roots:
  "https://gitlab.example.com/team/": "/srv/repos/team"

discovery_max_depth: 3
clone_parent: "data/agent-clones"
clone_cleanup: "always"
worktree_parent: "data/agent-worktrees"
shared_review_skill: "skills/review-agent/SKILL.md"
backend_timeout: -1
# agent_result_max_bytes is omitted by default (unlimited)
```

`repo_roots` keys may identify a project group or a complete project URL. Matching uses normalized URLs, longest path-boundary match, and the derived local path before bounded recursive discovery. An environment override may replace the shared skill path for Docker or external deployments.

## Review lifecycle

1. Parse the webhook's provider, project URL, source branch and MR/PR URL.
2. Resolve the local Source Repository from `repo_roots`.
   - Try the derived direct path first.
   - If missing, recursively inspect Git repositories only within `discovery_max_depth`.
   - Accept only a repository whose normalized `origin` matches the remote project.
   - If multiple repositories match, prefer the shallowest path, then lexical order.
3. Fetch the source branch from the source project and the target branch from the target project at job start. Record both revisions and review the source change from their merge base.
4. If no local repository matches, clone into `clone_parent`.
5. Ask the Agent to create a temporary Review Worktree under `worktree_parent`. The Agent chooses its child path, branch name and other Git details.
6. Read and execute the single shared review skill.
7. Review the complete current change from the target/source merge base. Use the previous/source range only to identify newly introduced changes for reporting. Then use the authenticated provider CLI (`glab`, `gh`, or the configured platform equivalent) to create or update the request's Rolling Review Note.
8. If a fix is clear and unambiguous, apply it and create a stacked fix MR/PR based on `SOURCE_REVISION` and targeting the original source project/source branch. Auto-fix remains enabled by default; it must not silently create a standalone change against the original target branch.
9. In a `finally`-equivalent cleanup path, remove the Review Worktree. Remove the clone by default; retain it only when configured.

The service must never checkout or modify the operator's source working tree directly. Local Agent and platform CLIs use the system installation, authentication, and permissions prepared for the worker account; OpenCode uses its remote execution environment. The service must not manage CLI login, token issuance, authentication refresh, or CLI authorization; it only supplies safe platform CLI example commands through the shared skill.

## Implementation phases

### Phase 1: Prompt and documentation contract

- Create `skills/review-agent/SKILL.md` from the current review prompt plus the defect-first rules from the Codex `review-agent` skill.
- Define the full-review/incremental-report contract and pass `TARGET_REVISION`, `SOURCE_REVISION`, and `PREVIOUS_REVIEWED_SOURCE_REVISION` explicitly. Require one Rolling Review Note per merge/pull request.
- Replace the current OpenCode prompt reference with the canonical skill path.
- Remove the obsolete `opencode/prompts/code-reviewer.md` after all references are migrated.
- Document in README that the skill is the core of Agent Review, how to override its path, how auto-fix is controlled by the prompt, and that platform CLI authentication is operator-owned.

### Phase 2: Repository and workspace resolution

- Implement YAML configuration loading and validation.
- Normalize remote URLs and enforce path-boundary matching.
- Implement bounded recursive Git repository discovery and exact `origin` verification.
- Add latest-branch fetch behavior, worktree lifecycle management, clone cleanup policy, and safe path containment checks.

### Phase 3: Explicit backend adapters

- Define an internal backend interface with explicit backends for `opencode`, `codex`, `claude`, and `pi`.
- Preserve the OpenCode Serve integration while passing the complete resolved task context and canonical skill path.
- Invoke local CLIs without shell interpolation, in their own process groups, with captured output and clear exit statuses.
- Give unattended backends the filesystem, Bash, and unrestricted network capabilities required by the Shared Review Skill through explicit backend-specific flags; do not depend on interactive approval prompts. Codex retains its native workspace sandbox, while Pi and permitted Claude Bash commands inherit the worker account's host permissions.
- Start Pi hermetically with project-local resources and automatic context loading disabled, the snapshotted Shared Review Skill loaded explicitly, and only the required built-in tools enabled.
- Preserve the selected backend's documented authentication, proxy, custom CA, and provider-routing environment without forwarding unrelated service secrets.
- Use system-installed and pre-authenticated Agent and platform CLIs in the selected backend's execution environment; local backends use the worker account and OpenCode uses the server environment. Do not implement a second permission model in this service. Put concrete, safe `glab`, `gh`, and supported-platform example commands in the Shared Review Skill.
- Before workspace preparation for a local CLI backend, check only that the selected Agent CLI and required platform CLI executables exist. Do not probe authentication or permissions. Treat a missing executable as a non-retryable configuration error. For a remote OpenCode backend, do not reject the job based on worker-local platform CLI availability; the CLI belongs to the remote execution environment.
- Fail closed when the selected backend is unavailable; do not silently switch to another Agent.

### Phase 4: Webhook integration and observability

- Route one Review Job to the configured backend.
- Normalize accepted webhooks in the web process and enqueue only the immutable execution fields; do not persist the complete webhook payload.
- Record backend, provider, project, Source Revision, Target Revision, Previous Reviewed Source Revision, Previous Review Note ID, `queued | running | completed | failed | timed_out` Execution Status, `not_attempted | confirmed | unconfirmed` Delivery Status, start/completion times, error, and cleanup error without deriving these fields from model output.
- Store each Agent Result as its native text, JSON, or JSONL output; do not require a shared model-generated JSON schema.
- Add duplicate-event/idempotency handling so repeated webhook deliveries do not create uncontrolled duplicate reviews.
- Build the transport idempotency key from provider, review URL, source revision hint, and target revision hint while ignoring action aliases. Supersede queued older revisions for the same review URL; do not force-stop one that already started.
- Replace one-process-per-webhook dispatch with a durable SQLite queue and a fixed worker pool governed by one global concurrency limit. Do not add per-backend quotas or an external broker in the first implementation.
- Keep the web process enqueue-only and expose an independently started worker command; do not start queue workers inside the web process.
- Run workers as the web service account by default, while allowing deployments to start them under a separately configured OS account.
- Mark a job `completed` only from a successful backend exit; do not treat that status as verified review-note or fix-MR delivery.
- Keep the existing Backend Timeout configuration scoped to Agent execution. Change its default to `-1` for unlimited execution; Git preparation, OpenCode session establishment, and cleanup keep independent positive timeouts. When a positive Backend Timeout expires, terminate the whole process group, wait for a short grace period, force-kill survivors, record `timed_out`, then give cleanup an independent bounded grace period.
- Retry infrastructure failures only when they occur before the Agent starts, with at most three retries and exponential backoff. Never automatically retry a job after the Agent has started, including `failed` and `timed_out`, because delivery may have partially succeeded.
- Store Agent Result in SQLite without a default application-level size limit. If `agent_result_max_bytes` is explicitly configured, retain its head and tail around an explicit truncation marker and persist `result_truncated=true`.
- Preserve partial stdout as Agent Result on backend failure and write redacted stderr to `error`; do not merge the streams. Record cleanup failures only in `cleanup_error` without changing the backend-derived Execution Status.
- When a worker lease expires after the Agent has started, mark the job `failed`, attempt orphan workspace cleanup, and do not requeue it.
- On SIGTERM, stop claiming, wait for a configurable shutdown grace period, then terminate remaining local Agent process groups with TERM followed by KILL after a bounded grace. Abort remaining OpenCode sessions through the server API. Mark those jobs `failed` and clean up.
- Retain completed Job records and Agent Results for 90 days by default, allow configuration, and delete expired rows in bounded maintenance batches.
- Create the first Rolling Review Note through the platform CLI and update the stored `previous_review_note_id` on later revisions. Use body files or stdin and complete non-interactive flags in canonical skill examples.
- Put a deterministic hidden marker in the note and write platform create/update responses to a fixed delivery receipt. Prefer the stored note ID, recover by marker when it is missing or deleted, and create a replacement only when recovery fails. If publication returns only plain text, reconcile by exact marker and accept the receipt only when exactly one current note matches.
- Keep the receipt as provider-native JSON and parse only note ID and URL in provider adapters. Do not ask the Agent to synthesize normalized receipt JSON.
- Parse the delivery receipt after every started Agent attempt, even if the backend fails or times out. Advance `previous_reviewed_source_revision` whenever that receipt confirms note creation or update. Keep backend `completed` semantics independent from delivery confirmation.
- Set Delivery Status to `not_attempted` before Agent start, `confirmed` for a valid native or reconciled receipt, and `unconfirmed` otherwise; never derive it from model prose or use it to overwrite Execution Status.
- Serialize jobs by review URL while retaining global concurrency across different reviews.
- Replace the automation-owned Rolling Review Note body with a current snapshot containing the current revision, new or changed findings, unresolved findings, and newly fixed findings; do not append unbounded per-revision history or preserve manual edits to that note.
- Assign and reuse simple finding keys such as `F001`, combined with semantic matching, without requiring structured finding JSON.
- If `PREVIOUS_REVIEWED_SOURCE_REVISION` is not an ancestor after a force-push, skip the invalid source delta, perform the full review, compare against the old note semantically, and mark the new snapshot as history rewritten.
- Remove or explicitly deprecate the legacy OpenCode client path so all production entry points use the same workspace and lifecycle service.

### Phase 5: Verification

- Unit-test configuration, URL normalization, longest-prefix mapping, bounded discovery, origin matching, candidate ordering, worktree cleanup and clone cleanup.
- Use fake `glab`, `gh` and Agent CLI binaries to test command construction and exit handling.
- Add backend smoke coverage for a non-Git job root, Codex network delivery, Claude Bash permissions, standard API-key and proxy/custom-CA environments, Pi's explicit tool set, ignored project resources, unrestricted network, and native result capture.
- Add a fork fixture where the upstream target branch advances independently, and assert the recorded Target Revision and merge base.
- Add sequential B-then-C revision coverage: both jobs inspect the full target/source diff, the second uses B..C only for reporting emphasis, and it updates the same Rolling Review Note.
- Test per-review serialization, receipt-gated previous revision advancement, missing/deleted note recovery by marker, bounded snapshot replacement, and force-push fallback.
- Test unlimited Backend Timeout, positive Agent-execution timeout, independent Git/session/cleanup timeouts, process-group termination, pre-Agent retry fencing and backoff, no retry after Agent start, unlimited results by default, and explicit head-and-tail result truncation.
- Test enqueue-time normalization without raw webhook retention, CLI existence-only preflight, missing-CLI non-retryability, partial stdout on failure, independent cleanup errors, and crashed-worker recovery after Agent start.
- Test action-alias idempotency, target-revision-sensitive keys, queued-revision supersession, graceful worker shutdown, and 90-day bounded retention cleanup.
- Test that resolved-revision second-layer deduplication compares only the latest confirmed snapshot, including a force-push or revert back to an older pair. Test provider-native receipt parsing after backend failure, all Delivery Status transitions, stable finding-key reuse, and automation-owned note replacement.
- Test OpenCode request payloads and canonical skill path propagation without a live server.
- Perform separate manual smoke tests for authenticated `glab`/`gh` execution and OpenCode Serve behavior; automated tests do not prove external CLI authentication or live MR publication.

## Explicit non-goals for the first phase

- LLM-based AgentRepoRouter-style automatic selection.
- Calling every installed CLI for the same MR.
- MCP-based review delivery.
- Service-managed authentication for `glab`, `gh`, `tea` or other platform CLIs.
- Service-managed CLI permissions or per-backend concurrency quotas.
- A fixed worktree name or fixed fix-branch naming convention.
