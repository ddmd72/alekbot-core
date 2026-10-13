# Decision: The drive's model extractor is Claude Haiku 5.5 or a GPT model behind a config switch, run by two agents on the indexer

**Date:** 2026-10-13
**Status:** Accepted (User Drive Step 2, Slice 2+4; owner decisions D7, D12)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §9, §24.2, §24.3, §24.9;
`src/agents/drive_document_reader_agent.py`, `src/agents/drive_image_triage_agent.py`,
`src/composition/drive_reader_runner.py`, `src/composition/drive_triage_runner.py`.

**Decision:** Scans, screenshots, images with text and PDFs the parser cannot read are transcribed by
`DriveDocumentReaderAgent` (`drive_reader`), which also returns a brief (type, one line, the document's date, parties,
amount). Every image is first triaged in batches by `DriveImageTriageAgent` (`drive_triage`): text to read, or a
photograph with a caption. Both are agents built per call with the user's own configuration, like the companion
extractors, and run only on the indexer service; their runners implement `ContentExtractorPort` and
`ImageTriagePort`, so the tick sees ports only. The provider is configuration: both default to Claude at ECO
(`claude-haiku-5-5`), and a user's `agent_providers` can switch either to OpenAI (`gpt-6-luna`). The pick is checked
live on the owner's real files before deploy (`scripts/validation/drive_model_check.py`). There is no fallback
provider: a second model's transcription was never checked, so an outage defers model work instead
([decision](drive_model_outage_defers_model_work.md)). Calls are billed to the user's account, run with provider-side
storage off, and are not copied into BigQuery or Logfire.

**Rejected alternatives:**
- *Gemini, as §9 once said:* the claim had no basis, and its adapter takes no inline file bytes.
- *An extension of `FileManagementAgent`:* that agent is zero-LLM and serves chat; the reader runs on the indexer
  with its own prompt, budget and billing.
- *A spike to choose the model first:* the provider is a switch, so the real files decide, not a synthetic benchmark.

**Trigger to revisit:** the live check or the eval bars favour the other provider, or a cheaper multimodal model reads
the hard cases (broken-font Cyrillic, UA/RU/ES scans) as well.
