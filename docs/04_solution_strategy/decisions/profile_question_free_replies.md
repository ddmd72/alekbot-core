# Decision: a profile question's free-text reply goes to the review model, which may answer the point (fact profile RFC §4.7)

**Decision.** Code reads a reply first, as before. Words it cannot read are recorded on the question (`reply_text`) and
acknowledged; the next review pass shows the model the fact with its `profile_question`. The model may decide the fact on
the words and may resolve the question's own point with `answer_profile_question(question_id, answer, quote)`. Code
validates it with the readers a digit or a button uses, records it as the owner's own answer and closes the question
`review`. Owner's map: `docs/05_building_blocks/memory/README.md` §5.6, "Profile questions in the review".

**Rejected.**
- Code-only replies: they cannot express a partial correction, such as a fact about the wrong subject.
- A cheap model that only maps the words onto a shown option: the same limit, it has no view of the fact.
- Leaving it to Stage 1 consolidation: its `memory_review_reply` rule leaves the quoted fact to the review.

**Traps guarded.**
- A profile question has no fallback and a timed-out one is never a candidate: silence archives nothing.
- One profile question per fact per pass (the earliest queued), so a decision on the fact closes exactly one.
- Housekeeping keeps a replied question open while its fact stands, so unread words reach one pass before the timeout.
- The review never writes a profile answer by itself: only through the tool, from words the owner wrote, re-checked
  against the stored question; a refused or failed record closes nothing.

**Revise when** the counters show the model answering points the owner would have picked differently, or a profile kind
needs a value the readers do not accept.
