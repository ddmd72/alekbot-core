# Decision: The drive's model extractor is a GPT or Claude model behind a config switch, with a Gemini Flash fallback, run by two agents on the indexer

**Date:** 2026-10-13
**Status:** Accepted (User Drive Step 2, Slice 2+4; owner decisions D7, D12); amended 2026-10-13 after the live check
(D7 rev 3.17: GPT-6 Luna default, Gemini Flash fallback, 800 px triage thumbnails)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §9, §24.2, §24.3, §24.9;
`src/agents/drive_document_reader_agent.py`, `src/agents/drive_image_triage_agent.py`,
`src/composition/drive_reader_runner.py`, `src/composition/drive_triage_runner.py`.

**Decision:** Scans, screenshots, images with text and PDFs the parser cannot read are transcribed by
`DriveDocumentReaderAgent` (`drive_reader`), which also returns a brief (type, one line, the document's date, parties,
amount). Every image is first triaged in batches by `DriveImageTriageAgent` (`drive_triage`): text to read, or a
photograph with a caption. Both are agents built per call with the user's own configuration, like the companion
extractors, and run only on the indexer service; their runners implement `ContentExtractorPort` and
`ImageTriagePort`, so the tick sees ports only. The provider is configuration: both default to OpenAI at ECO
(`gpt-6-luna`); a user's `agent_providers` can switch either to Claude (`claude-haiku-5-5`), and so does the user's
global `provider_preference` when it names an allowed provider and the agent has no override of its own (the usual
`AgentContextBuilder` order). The pick was checked live on the owner's photos before deploy
(`scripts/validation/drive_model_check.py`, 2026-10-13): Claude Haiku 5.5 shifted the captions of a 16-image triage
batch by one image in 1 call of 5 (11 captions on the wrong files), GPT-6 Luna shifted none in 8, at a third of the
cost, and at 800 px Luna's captions were more precise than at 176 px — so triage sends the 800 px thumbnail.

**Fallback (owner decision 2026-10-13):** both agents fail over to Gemini Flash (`gemini-3.8-flash`): a call the
base layer fails over on (429, 5xx, timeout, network, open breaker) is re-served there; only when both fail is it an
outage that defers model work ([decision](drive_model_outage_defers_model_work.md)). The model is pinned through a
new optional strategy key, `fallback_tier: BALANCED`, which `AgentContextBuilder._build` uses to resolve the
fallback's model; without it the fallback would resolve at the agents' ECO tier, which on Gemini is
`gemini-3.5-flash-lite`, not the Flash the owner chose. The Gemini adapter gained inline file bytes for this (it
dropped base64 file parts before). Calls are billed to the user's account at the model that ran, run with
`allow_provider_storage=False` (OpenAI's `store=false`: no copy kept for retrieval or the dashboard; not a
zero-retention guarantee, and Claude and Gemini have no such flag), and are not copied into BigQuery or Logfire.

**Rejected alternatives:**
- *Gemini as the extractor, as §9 once said:* the claim had no basis. Gemini as the *fallback* is a different
  claim: it reads only when the checked primary cannot, which beats deferring all model work.
- *Claude Haiku 5.5 as the default (the first pick):* it shifted a triage batch's captions in the live check.
- *No fallback (the first decision):* replaced on the owner's decision; a primary outage no longer stops reading.
- *Pinning the fallback by model id in the strategy:* a model id in `services/` bypasses the adapter's tier map and
  its env overrides; a tier keeps the adapter the owner of the id.
- *An extension of `FileManagementAgent`:* that agent is zero-LLM and serves chat; the reader runs on the indexer
  with its own prompt, budget and billing.
- *A spike to choose the model first:* the provider is a switch, so the real files decide, not a synthetic benchmark.

**Trigger to revisit:** the live check or the eval bars favour another provider, a cheaper multimodal model reads
the hard cases (broken-font Cyrillic, UA/RU/ES scans) as well, Gemini 3.8 Flash's promotional price ends
(2026-12-31), or failover calls show up often enough in `[drive_reader]`/`[drive_triage]` logs that the fallback's
quality needs its own live check.
