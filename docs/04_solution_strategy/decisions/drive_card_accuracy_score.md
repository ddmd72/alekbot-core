# Decision: One composite card accuracy score, native topics at weight 0, the 90 % bar only in the skill

**Date:** 2026-10-19
**Status:** Accepted (arbiter, after the cross-cutting review of the §26 build; D35, D36, Δ61, Δ89).
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §21 D35–D37, §26.4;
`src/domain/drive_accuracy.py`, `src/skills/smart/drive-search/SKILL.md`.

## Decision

A card carries **one** score, `accuracy = clamp(round(100 · (1 − S) · (1 − V)) − R, 0, 100)`, computed by code from
graded values (V), the text's capture (S) and reading problems (R); every weight lives in one hashed table under
`ACCURACY_VERSION`. **Native topics are graded and stored but weigh 0** (Δ89): they are an interpretation in the user's
language, and at weight 1 most fully confirmed cards fell under 90 %, which would have Smart open almost every file.
**The 90 % bar exists only in the skill text** (D36): Smart opens a file for a value question below it, never for a
find. A file a model read and called `digital` is priced 0.03, not the native 0 (Δ85).

## Rejected

- **Per-field detail to Smart** — more prompt tokens, and Smart would reason about fields instead of applying one rule.
- **Dropping unverified values** (the old `verify`) — a value written differently was lost from search; the score prices
  the doubt instead and the value stays searchable, never exact.
- **A bar in code (a gate that hides cards under N %)** — a low score is information for Smart, not a reason to hide a
  file; the bar would need a deploy to tune.
- **Native topics at weight 1** — see above; the review showed the 90 % bar then fires on nearly every card.
- **Score inside the card text** — every weight change would re-embed every card; kept outside it so a bump re-scores
  from stored grades only.

## Revisit when

The revision script (D37) shows a class that scores low throughout (a weight or a prompt to fix), recurring
`not_in_text` shapes (a format the grader misses), or live G3 runs where Smart opens files it need not, or answers a
wrong value from a card at 90 % or more: move the weights (an `ACCURACY_VERSION` bump) or the bar in the skill.
