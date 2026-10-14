# Decision: Audio and video stay metadata-only in Slice 2+4; transcripts come later through the extractor seam

**Date:** 2026-10-13
**Status:** Accepted, deferred (User Drive Step 2; owner, 2026-10-13)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §14, §24 (opening paragraph),
§5.1; `SLICE2_CONTENT_KINDS` and `EXTRACTOR_CHAINS` in `src/domain/drive_index_policy.py` and
`src/domain/drive_extraction.py`.

**Decision:** Audio and video files keep their metadata card (name, kind, duration, date, place) and are not
admitted for content: the slice admits only kinds with a named extractor. VTT/SRT transcripts saved next to a
recording are admitted and searchable. Long-audio and video transcripts are a later stage, added as an extension:
a transcription link in `EXTRACTOR_CHAINS` for `audio`/`video` behind `ContentExtractorPort`, the kinds added to the
admitted set, and a `policy_version` bump so the upgrade pass makes the existing records due. Nothing in the record,
chunk or search model changes.

**Rejected alternatives:**
- *Reuse the chat path's `AudioTranscriptionPort` now:* chat transcribes one short voice note per turn; a drive holds
  hour-long recordings whose cost, chunking by time and speaker turns need their own design and budget.
- *Admit audio with an empty extractor:* every recording would read as `no_text` and count as unread forever.

**Trigger to revisit:** the owner asks for search inside recordings, or meeting recordings become a common drive
content.
