# Lelik's call opening: one keyed user note from the main service

Status (2026-11-21): decided, built.

**Decision.** A LELIK-mode call opens with a note the main service builds (`LelikAgent.session_config`, texts in
`src/domain/voice_opening.py`), wrapped with `wrap_system` under a per-call key that is also rendered into the
instructions as `session_key`. The relay sends it as the first user item and then no pickup note. The rule that reads
the key is `POLICY_SYSTEM_PROVENANCE` in the `lelik` and `lelik_xai` profiles (`provenance` first in the Lelik
blueprint). Interpreter and tutor get no key and no note; a config without the note keeps the pickup note; a resumed call
gets neither. `VOICE_WEB_CALLER_OPENING=off` (relay) makes web calls ignore the note.

**Why.** A call has no user turn and its history holds summaries of earlier, mostly silent calls, so the model took the
silence and those summaries for the subject and remarked on the line. A system note that explained the pickup did not
help; a keyed user note did. The pickup note's "one short sentence" contradicted the opening's ask for something to
answer, so the two never go in together.

**Language rule.** Fixed-language user: the note in that language (preferred, else `SYSTEM_DEFAULT_LANGUAGE`, as the
`LANG_FIXED_*` token is chosen), no language sentence. Mirror user: the English text, plus one sentence naming the
language of the caller's latest message, only when the history holds one the caller wrote (the code-written
`[System: …]` records, such as call summaries, do not count).

**Rejected.** A system-role note (did not help). The relay's English constant as a user turn (in mirror mode it was
mirrored as the caller's language). A localized opening without the key (nothing marks it as the system's).
