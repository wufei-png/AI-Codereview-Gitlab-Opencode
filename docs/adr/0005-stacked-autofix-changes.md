# Stacked Auto-fix Changes

**Status: accepted.** An automatic fix belongs to the merge/pull request that exposed the finding. The Agent creates a disposable fix branch from the current `SOURCE_REVISION`, then creates a stacked fix MR/PR whose target is the original `SOURCE_PROJECT_PATH` and `SOURCE_BRANCH`. The original review request remains the user-facing change; merging the fix advances its source branch and triggers the normal next review.

For fork requests, the source project is the fork that owns the original source branch. The fix must target that source project and branch, not the upstream `TARGET_PROJECT_PATH` or `TARGET_BRANCH`. The service continues to use `TARGET_REVISION` from the target project as the review base; fix targeting does not change merge-base correctness or target revision fetching.

The Agent must not silently fall back to a standalone fix MR targeting the original target branch. If the provider cannot create the stacked change, or the source branch is unavailable, it reports the fix as undelivered and leaves the original source unchanged. A standalone replacement may be created only by an explicit future operator policy, not by the default auto-fix path.
