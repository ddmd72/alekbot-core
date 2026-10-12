# Decision: Drive date filters cut days at UTC midnight

**Date:** 2026-10-12
**Status:** Accepted, deferred (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §3.1, §10.1;
`request_from_fields` in `src/domain/drive_search.py`, `effective_date` in `src/domain/drive_card.py`.

**Decision:** A date-only `drive_date_from` / `drive_date_to` is a whole UTC day, and `effective_date` is compared as
stored. **Verified fact:** Graph's `photo.takenDateTime` is the camera's local wall clock labelled `Z`, not UTC. On
IMG_6172 the EXIF/Spotlight time was 13:13:40 UTC and Graph returned 15:13:40Z, the local wall clock. So a photo's
stored date already reads as the user's local time, and the UTC cut gives the day the user means. Only true-UTC
fallbacks (`fileSystemInfo.lastModifiedDateTime`, the server time) are off by the user's offset, which matters only
for a file saved within that many hours of midnight.

**Rejected alternatives:**
- *Shift the bounds by `UserBotConfig.timezone`:* fixes true-UTC dates and breaks photo dates, which are already
  local; it would need a per-field rule keyed on which fallback produced the date.
- *Store a local calendar date at index time:* needs the user's zone in the indexer and goes stale when the user
  travels or changes it; a photo's own zone is not in the facet at all.
- *Correct `takenDateTime` with the location facet's zone:* a time-zone lookup per photo for a one-hour edge case.

**Trigger to revisit:** a user's "files from yesterday" misses or adds a file saved near midnight, a provider whose
taken time is true UTC, or Slice 2 admitting documents whose dates come from their content.
