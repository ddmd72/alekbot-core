# HtmlPageGenerator: drop the fixed benchmark catalogue, ground design in the subject

**Date:** 2026-08-21
**Status:** Accepted — live trial, 1–2 weeks starting 2026-08-21. Revisit after the trial;
not yet a settled decision.
**Scope:** `prompts_snapshot/tokens/system/COGNITIVE_PROCESS_HTML_PAGE.groovy` (Firestore
token `COGNITIVE_PROCESS_HTML_PAGE`), `scripts/html_page/test_design_patch.py`,
`firestore_utils/backup_prompt_layer.py`

## Context

The daily morning briefing (`daily_email_review` → `create_html_page`, fires ~06:08 UTC
every day, live on Grok since 2026-08-15) had been visibly monotonous for at least a
week: BigQuery `prompt_content` showed 2026-08-19 and 2026-08-20 producing a **byte-identical
font stack** (Newsreader/Source Serif 4/Libre Franklin), and every sampled day converged on
a near-black-or-near-white ground with a red accent.

The prior mechanism (`step_2_narrow` → `step_3_pick`, shipped ~2026-03, patched
2026-08-20/21 with `step_1b_emotion`/`step_4b_lateral`/`avoid_ai_cliches`) forced the model
to pick a domain from a fixed list, then imitate one named real-world benchmark from
`StyleCatalogue` (e.g. NYT, The Economist, Stripe, Magnum Photos). For document/report
content the usable options were effectively just NYT and The Economist — Wired/Verge/
FiveThirtyEight/education/fintech domains don't fit grim daily news, so the model had
nowhere else to go.

External research the same day (market-trend check, see
[[project_docgen_trend_check_2026_08]]) had already surfaced that Anthropic's own Agent
Skills design guidance leads with "ground it in the subject," not benchmark imitation —
this decision applies that principle here.

## What was tried

Three variants, tested with `scripts/html_page/test_design_patch.py --variant <x>` against
the same real production input (the 2026-08-20 06:15 UTC briefing delegation, extracted
from BigQuery `prompt_content`, 26.8k chars) on Grok:

1. **`base`** — the emotion/lateral/anti-cliché patch alone, catalogue intact. → NYT again
   (Playfair Display / Source Serif 4), same register as the unpatched production runs.
2. **`sitrep`** — added a 5th domain option to the catalogue for (B) Document/report
   (`intelligence_briefing`: Bloomberg Terminal, declassified cable, Stratfor brief, NATO
   SITREP). → **model did not select it** — still picked NYT, same fonts. The new option
   sat unused; adding a choice does not change what gets chosen when the existing options
   already "win" the model's own scoring.
3. **`nodomain`** — removed `StyleCatalogue` and the domain-selection steps entirely;
   replaced with subject-grounded design-plan derivation. → produced a genuinely different,
   non-benchmark result: "a newsstand at 6am, ink still wet," navy/tan/cherry-red palette,
   IBM Plex, an editor's proof-sheet with struck-through postponed stories as the structural
   device for urgency. No console errors, responsive, sources still linked inline.

Owner's read across this and four other one-off tests that same session (personal essay ×2
providers, e-commerce landing, RPG team page): `nodomain` was preferred over the
benchmark-anchored baseline on the one content type that had shown real monotony
(document/report); `base` had already shown good results on other content types
(commerce, creative/personal).

## Decision

Ship `nodomain` for `html_page` broadly (not scoped to document/report only), with subject
grounding elevated to a **MANDATORY** guardrail — not just a process step:

```groovy
subject_grounding: [
    "MANDATORY. Every visual decision ... must derive from THIS specific subject's own
    materials, instruments, textures, and vernacular. Never imitate an existing brand,
    publication, or product's identity, even implicitly or unconsciously.",
    "If the result could be described as 'looks like <existing company/publication>', it
    has failed this constraint. Discard that direction and re-derive from the subject itself."
]
```

placed in `TechnicalGuardrails` (same enforcement tier as "no horizontal scrolling"), plus a
matching check added to `step_5_audit`. `step_2_narrow`/`step_3_pick` (domain pick + named
benchmark) are removed; `step_2_ground` replaces them. `step_1b_emotion`, `step_4b_lateral`,
and `avoid_ai_cliches` (added 2026-08-20/21) are kept — this is additive on top of that
patch, not a reversion of it.

## Alternatives considered

- **Add more catalogue domains/benchmarks** (the `sitrep` variant) — rejected. Directly
  tested: the model did not reach for the new option even when it existed and was tonally
  appropriate. The problem was never "not enough named benchmarks," it was the
  benchmark-imitation mechanism itself narrowing toward whichever 1–2 options score best
  for a given register — adding more rows to that same table doesn't change the scoring.
- **Keep the catalogue, force rotation/anti-repeat memory** — not attempted. Would need the
  agent to see its own prior output (it currently doesn't — each generation is stateless),
  a bigger architectural change than swapping one step. Left for a future session if the
  trial below doesn't hold up.

## Trial plan / open risk

This is one sample per variant on one day's content — the codebase has prior evidence that
LLM behavior on identical repeated input can have large run-to-run spread (see
`decisions/directive_applicability_gate.md` § Variance, 0 vs 6 demotions on identical
input in a different subsystem). Whether `nodomain` actually avoids *its own* day-to-day
repetition (the exact failure mode being fixed) is not yet known — that is what the 1–2
week live trial is for. Revisit this record with real multi-day production data before
calling it settled either way.

## Rollback

`scripts/memory/prompt_tokens_firestore_backup_20260821T104943Z.json` — full Firestore
prompt-layer dump taken immediately before this push (gitignored, PII-safe, per
`prompts_snapshot/README.md` "Backup before bulk pushes"). Restore path if the trial fails:
re-upload the pre-change token content via `snapshot_upload.py --apply` using this backup,
or pull the pre-2026-08-21 T2 revision from git history of
`prompts_snapshot/tokens/system/COGNITIVE_PROCESS_HTML_PAGE.groovy`.
