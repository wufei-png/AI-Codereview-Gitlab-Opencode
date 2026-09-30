# Delivery Receipt Reconciliation for Rolling Review Notes

**Status: accepted.** A Rolling Review Note is confirmed by a provider-native delivery response whenever the platform CLI or API returns one. When a provider publishes the note but returns only plain text, the Agent performs a post-delivery reconciliation against the target review's comments/notes using the exact deterministic review marker.

The reconciliation is successful only when exactly one current automation-owned note matches the marker and exposes a valid provider note ID and both current revisions in its body; native URLs are checked when present. The Agent writes that provider-native note object unchanged to `DELIVERY_RECEIPT_PATH`; it must not synthesize a boolean or text-based success receipt. Zero matches, multiple matches, a missing identifier, or an inability to establish the current snapshot leaves Delivery Status as `unconfirmed`.

The durable queue continues to use only `confirmed` deliveries for `Previous Reviewed Source Revision`, note-ID reuse, and resolved-revision deduplication. This preserves safe history semantics when a publish may have happened but cannot be uniquely identified. The trade-off is that an ambiguous provider state can produce a later duplicate note; resolving that ambiguity is safer than silently updating the wrong note.


## Local snapshot validation (2026-09-30)

ADR-0007 retains Agent-owned publishing. For new executions, the framework reads the unchanged native note object locally: a positive note ID, exactly one exact hidden review marker, and both complete authoritative revisions in the body are required. Recognizable native review/repository URLs and GitLab `noteable_iid` must agree with the target request. GitLab `noteable_id` is a global database ID and is not compared to the MR IID. A native GitLab response may lack a browser URL; the known note ID is sufficient with matching body/context.

When the body uses recognizable current Source Revision or Target Revision labels, the labelled full SHA must match its corresponding snapshot. A requested SHA mentioned elsewhere in history cannot override a contradictory current field. These labels remain optional; common Markdown formatting and unlabelled native prose remain supported without a new Agent output template.

`confirmed` means a snapshot-matching response supplied by the trusted Agent; it does not claim provider readback or that the current remote heads still match. No new Agent fields or normalized result schema are introduced. Missing/invalid receipts remain `unconfirmed`; available raw JSON/text is retained in `delivery_receipt`, with a separate `delivery_error`. Backend execution and workspace cleanup errors remain independent. A failed backend with a valid receipt can still confirm delivery. Unconfirmed delivery neither advances history nor retries an already-started Agent. Existing confirmed rows are not reclassified.
