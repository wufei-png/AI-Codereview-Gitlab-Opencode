# Delivery Receipt Reconciliation for Rolling Review Notes

**Status: accepted.** A Rolling Review Note is confirmed by a provider-native delivery response whenever the platform CLI or API returns one. When a provider publishes the note but returns only plain text, the Agent performs a post-delivery reconciliation against the target review's comments/notes using the exact deterministic review marker.

The reconciliation is successful only when exactly one current automation-owned note matches the marker and exposes a provider note ID and URL. The Agent writes that provider-native note object unchanged to `DELIVERY_RECEIPT_PATH`; it must not synthesize a boolean or text-based success receipt. Zero matches, multiple matches, a missing identifier, or an inability to establish the current snapshot leaves Delivery Status as `unconfirmed`.

The durable queue continues to use only `confirmed` deliveries for `Previous Reviewed Source Revision`, note-ID reuse, and resolved-revision deduplication. This preserves safe history semantics when a publish may have happened but cannot be uniquely identified. The trade-off is that an ambiguous provider state can produce a later duplicate note; resolving that ambiguity is safer than silently updating the wrong note.
