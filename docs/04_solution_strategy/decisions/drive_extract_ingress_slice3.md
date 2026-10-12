# Decision: How the main service reaches the indexer's `/extract` is decided in Slice 3

**Date:** 2026-10-12
**Status:** Deferred (User Drive Step 2; Slice 3)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §9 (Slice 3: the main service calls an
OIDC-protected `/extract` on the indexer), §12.5, RFC Δ21;
[drive_indexer_lease_queue_service.md](drive_indexer_lease_queue_service.md).

**Decision:** Slice 1 runs the indexer with `--ingress=internal` plus the in-app OIDC check, because its only caller
is Cloud Tasks in the same project, which Cloud Run counts as internal. Slice 3 adds a second caller, the main
service, calling synchronously. Per Cloud Run's ingress rules a request from another Cloud Run service counts as
internal only when it leaves through a VPC (Direct VPC egress or a Serverless VPC Access connector); the main service
has no VPC egress today. The choice is left to Slice 3, with its own measurements; nothing in Slice 1 depends on it.

**Options for Slice 3 (none chosen):**
- *VPC egress on the main service, ingress stays internal:* keeps the network-level defence; adds a VPC, egress
  settings that also route the bot's Slack, Telegram and LLM traffic (or a private-ranges-only split), and cost.
- *Indexer ingress `all`, OIDC only, as the main `/worker`:* no new infrastructure; the network layer is gone and
  the pinned-audience token check is the whole gate.
- *A separate extraction service with its own ingress:* a fourth deploy unit for one route.
- *Extraction through Cloud Tasks:* asynchronous, so not usable on the chat path.

**Trigger to revisit:** the Slice 3 design (one extraction path for chat uploads, `open_file_from_drive` and email
attachments).
