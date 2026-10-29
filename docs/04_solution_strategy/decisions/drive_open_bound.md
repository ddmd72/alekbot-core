# Drive opens are bounded at 60 000 characters (2026-10-29)

**Decision (owner rule; design `USER_DRIVE_INDEXING_RFC.md` §29, rev 3.34; built: no).** A text file from the user's drive
above `OPEN_MAX_CHARS` (60 000) is never handed to a model whole, on any origin, through either caller (`open_drive_file`,
`open_file` on a `drive:` ref), with no override. When the index can page it, the open is refused before any download and
the file is read through the index: `find_passages` by meaning first, `read_passages from N` a page at a time only when the
user's own words ask for the text in order. Otherwise the open returns the head (at a paragraph) and a code-written cut
line. A stale file opens live and cut; its line names the old indexed version, which stays searchable. Live transcripts are
refused then paged; archived transcripts keep head-and-cut. Vision opens are unchanged and only logged. This reverses
A16 and resolves OQ-2.

**Why.** On 2026-10-29 a call asked for the first page of a long document; one run of three opened the whole very long
file, and the orchestrator's next request was many times the cap. The only guard was one model telling a method phrase
from a need phrase in another model's commission. A code bound does not depend on that reading.

**Rejected.**
- Head-and-cut for indexed files too: returns what page 1 already holds and gives nothing to go on with.
- Send the file to the chat: answers "give me the file", not "what does it say"; out of scope.
- A model-decided ask-back status: a second loop for a decision code makes deterministically.
- Uncapped with sharper wording: run-to-run variance found the hole; the next paraphrase would too.

**Revisit when** the `[DriveOperations] open refused` / `open cut` lines show ordinary documents refused or cut often
(the cap is too low); a long-document eval shows whole-work questions failing from passages (§28.7 chapter summaries);
or the `opened vision` line shows large born-digital PDFs reaching Smart whole more than occasionally (§29.11).
