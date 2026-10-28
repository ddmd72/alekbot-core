# Decision: A low-accuracy value is read through passages when the text is born-digital, by an open when it is image text

**Date:** 2026-10-28
**Status:** Accepted (owner, OQ-A2, 2026-10-28); built on `feat/drive-answer-from-content`, not deployed.
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §28.2, §28.3 A10, A13, §28.6 OQ-A2;
[drive_card_accuracy_score.md](drive_card_accuracy_score.md); code `src/domain/drive_capture.py` (`IMAGE_TEXT_CAPTURES`),
`ReadFacts.low_accuracy_image`, `R_ACCURACY_VALUE`, `R_ACCURACY_IMAGE` in `src/domain/drive_answer_texts.py`.

**Decision:** for a value (a number, a date, an amount) in a file whose card accuracy is below 90 %:
- **Born-digital text** (`capture == "digital"`): the stored chunks are the file's own text, so the answer is the
  file's passages on that value (`R_ACCURACY_VALUE`: "ask for the file's passages on that value").
- **Image text** (`capture` scan, document photo or screenshot) **or no capture recorded**: the stored text is a reading of
  an image, so passages repeat the same reading; the answer is an open of the file (vision). Unknown capture is treated as
  image text.
- **Who decides:** code. `ReadFacts.low_accuracy_image` (a card below `CARD_ACCURACY_TRUST` whose capture is in
  `IMAGE_TEXT_CAPTURES` or missing) selects the Smart reading line `R_ACCURACY_IMAGE`; the passages head line carries the
  mark for the specialist (` · read from an image`, ` · capture unknown`, ` · card accuracy N%`). Never at 90 % or more,
  never for a digital card; the reader needs no search first.

**Rejected alternatives:**
- *Passages always:* repeats a mis-read value from a scan with the authority of a quote.
- *An open always* (the state before this change): pays a download, a conversion and the whole text in Smart's context for
  files whose own text is exact.
- *A recorded-capture-only rule* (unknown opens nothing): a file with no capture would silently skip the safe route.

**Trigger to revisit:** the S3 `low_accuracy` family shows a born-digital file opened anyway, or capture detection starts
mislabelling mixed PDFs often enough to matter.
