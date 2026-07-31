# OpenCode Agent and Multi-CLI Review Optimization

## Decision summary

The project will retain OpenCode Serve and add explicitly selected Codex CLI and Claude CLI backends. The first phase will not perform automatic CLI routing or fan-out. The Agent remains responsible for posting review notes and creating an optional fix MR through an already-authenticated platform CLI; MCP is out of scope.

The project will maintain one canonical prompt/skill at `skills/review-agent/SKILL.md`. OpenCode, Codex and Claude invocation wrappers only provide the task context and the resolved skill path; they must not duplicate review policy.

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
```

`repo_roots` keys may identify a project group or a complete project URL. Matching uses normalized URLs, longest path-boundary match, and the derived local path before bounded recursive discovery. An environment override may replace the shared skill path for Docker or external deployments.

## Review lifecycle

1. Parse the webhook's provider, project URL, source branch and MR/PR URL.
2. Resolve the local Source Repository from `repo_roots`.
   - Try the derived direct path first.
   - If missing, recursively inspect Git repositories only within `discovery_max_depth`.
   - Accept only a repository whose normalized `origin` matches the remote project.
   - If multiple repositories match, prefer the shallowest path, then lexical order.
3. Fetch the source branch at job start and review its latest remote revision.
4. If no local repository matches, clone into `clone_parent`.
5. Ask the Agent to create a temporary Review Worktree under `worktree_parent`. The Agent chooses its child path, branch name and other Git details.
6. Read and execute the single shared review skill.
7. Review the change, use the authenticated provider CLI (`glab`, `gh`, or the configured platform equivalent), and create the review note.
8. If a fix is clear and unambiguous, apply it and create a fix MR according to the Agent's platform-native conventions. Auto-fix remains enabled by default.
9. In a `finally`-equivalent cleanup path, remove the Review Worktree. Remove the clone by default; retain it only when configured.

The service must never checkout or modify the operator's source working tree directly. It also must not manage CLI login, token issuance, or authentication refresh.

## Implementation phases

### Phase 1: Prompt and documentation contract

- Create `skills/review-agent/SKILL.md` from the current review prompt plus the defect-first rules from the Codex `review-agent` skill.
- Replace the current OpenCode prompt reference with the canonical skill path.
- Remove the obsolete `opencode/prompts/code-reviewer.md` after all references are migrated.
- Document in README that the skill is the core of Agent Review, how to override its path, how auto-fix is controlled by the prompt, and that platform CLI authentication is operator-owned.

### Phase 2: Repository and workspace resolution

- Implement YAML configuration loading and validation.
- Normalize remote URLs and enforce path-boundary matching.
- Implement bounded recursive Git repository discovery and exact `origin` verification.
- Add latest-branch fetch behavior, worktree lifecycle management, clone cleanup policy, and safe path containment checks.

### Phase 3: Explicit backend adapters

- Define an internal backend interface with explicit backends for `opencode`, `codex`, and `claude`.
- Preserve the OpenCode Serve integration while passing the complete resolved task context and canonical skill path.
- Invoke local CLIs without shell interpolation, with bounded timeouts, captured output, and clear exit statuses.
- Fail closed when the selected backend is unavailable; do not silently switch to another Agent.

### Phase 4: Webhook integration and observability

- Route one Review Job to the configured backend.
- Record backend, provider, project, branch, worktree path, clone ownership, start/end status and cleanup result without logging secrets.
- Add duplicate-event/idempotency handling so repeated webhook deliveries do not create uncontrolled duplicate reviews.

### Phase 5: Verification

- Unit-test configuration, URL normalization, longest-prefix mapping, bounded discovery, origin matching, candidate ordering, worktree cleanup and clone cleanup.
- Use fake `glab`, `gh` and Agent CLI binaries to test command construction and exit handling.
- Test OpenCode request payloads and canonical skill path propagation without a live server.
- Perform separate manual smoke tests for authenticated `glab`/`gh` execution and OpenCode Serve behavior; automated tests do not prove external CLI authentication or live MR publication.

## Explicit non-goals for the first phase

- LLM-based AgentRepoRouter-style automatic selection.
- Calling every installed CLI for the same MR.
- MCP-based review delivery.
- Service-managed authentication for `glab`, `gh`, `tea` or other platform CLIs.
- A fixed worktree name or fixed fix-branch naming convention.
