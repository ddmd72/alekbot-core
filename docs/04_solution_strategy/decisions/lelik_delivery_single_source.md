# Lelik's delivery guidance lives in one place: the anchor

**Date:** 2026-09-28
**Status:** Accepted (owner decision after a live web call: "a radical improvement").

## Decision

How Lelik sounds is set in exactly one place, the spoken paragraph of `build_persona_anchor`
(`src/domain/llm.py`), sent before every reply. It names a register the model already knows as a
whole — a regular person on a spontaneous phone call — and describes it briefly: uneven pacing,
pitch rises on the words that matter and trailing off at sentence ends, occasional disfluencies,
tone that follows the content ("felt, not performed"), plus the live-speech wording gate and a
language line ("the configured language is the default, not a lock").

The other two places are reduced to one job each:

- `SPOKEN_DELIVERY` (Firestore token) keeps call mechanics only: unreadable medium (no lists,
  markdown, URLs), confirm before irreversible actions, silence check and keeping the caller company
  during a delegation. It still has to exist: the anchor's spoken paragraph is gated on a
  `spoken_delivery {` section being present, and the relay's silence note relies on its rule.
- `_CALLER_OPENING` (`relay_main.py`) is only a cue to speak first, in English per repo rules, with
  a reminder to follow language settings so `LANG_MIRROR` does not mirror the English cue.

## Why

Three sources contradicted each other: the token asked for slow-downs and beats of silence, the
Russian-language opening asked for no pauses and more speed, the anchor asked for natural pauses and
no performed emotion. Per-sentence prosody rules (the token's `rhythm`/`tone_of_voice`) read as a
narrator on every live call. Naming a register instead of assembling it from rules fixed it.

## Rejected

- **A separate `Emotion` section.** Duplicates the Tone line and adds weight the model reads as "act
  more"; the 2026-09-25 "no theatre" fix already showed that. Folded into Tone as "felt, not performed".
- **Mandatory disfluencies** (as in the source draft). The anchor repeats every turn, so a mandatory
  false start becomes a tic. They are "now and then".
- **"Mildly distracted" and English example fillers** from the source draft: the first bleeds into
  comprehension, the second gets copied verbatim into non-English speech.
- **Hard-coding "except Russian" in the language line.** The anchor is shared code; the ban already
  comes from the owner's `LANG_FIXED_UK`, so the line defers to "languages the settings rule out".
- **Relaxing `LANG_FIXED_*` itself.** Those tokens are shared with Smart's text path, where language
  changes go through `LanguagePreferenceService`; the fix belongs to the voice-only anchor.

## Rollback

The full previous `SPOKEN_DELIVERY` content is backed up locally in `scripts/memory/` (gitignored).
Restore it in Firestore and revert the anchor/opening commits.
