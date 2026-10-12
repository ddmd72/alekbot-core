# Slice 1 overnight run log (cloud session, 2026-10-12)

Plan: `docs/superpowers/plans/2026-10-12-user-drive-indexing-slice1.md`. Scope: Tasks 0–46. Task 47 waits for the owner.
Method: subagent-driven (implementer → independent reviewer → coordinator acceptance with `make check`).

## Setup

- Python 3.13.16, fresh `venv/` from `requirements.txt`.
- `make` uses `PYTHON ?= python3`, so every `make` runs with `PATH=$PWD/venv/bin:$PATH`.
- Baseline `make check` before Task 0: **green — 6934 passed, 3 skipped, 1 xfailed**.
- No `.env` in this environment.

## Tasks


### Task 0 — done
- Commit `ef048c3`. `make check` (coordinator): 6940 passed, 3 skipped, 1 xfailed.
- Deviations: none. Fixture copied byte-identical from the plan (reviewer diffed all four blocks).
- Reviewer: APPROVED, round 1.
- **OWNER decision (fixture slip in the plan):** in `tree.json` only `Photos/Porto/IMG_6201.HEIC` is at Porto
  (41.1579, -8.6291). `IMG_6202..6206` have latitude 39.4705..39.4729 (Valencia's latitude, Porto's longitude), which
  Δ7 resolves to "Monsanto, Portugal". Card text has no path, so gold cases `place-porto-en` / `place-porto-ru`
  (expect all six) are reachable for 1 of 6 only. Left verbatim (the gold file is written before the code and is not
  the implementer's to bend). Suggested fix: latitudes ≈ 41.1585..41.1609 (+0.0006 step) **before** the first
  `--record` of bars in Task 47.

### Task 1 — done
- Commit `9e6985d`. `make check` (coordinator, clean worktree at the commit): 6947 passed, 3 skipped, 1 xfailed.
- Deviations: none (all three files byte-identical to the plan).
- Reviewer: APPROVED, round 1. `drive_index.py` holds 14 public classes as the plan specifies; the reviewer accepted
  it (existing domain model modules do the same).
- Side finding (not this plan): `tests/unit/adapters/test_firestore_account_repo_lock_free.py` captures `NOW` at
  import, so a full run crossing midnight UTC fails 6 tests. Seen once in the reviewer's run, green on rerun. Queued
  as a separate suggested task, untouched here.

### Task 2 — done
- Commit `cc2b2d3`. `make check` (coordinator, worktree at the commit): 6957 passed, 3 skipped, 1 xfailed.
- Deviations: none (byte-identical to the plan). Reviewer: APPROVED, round 1.

### Task 3 — done
- Commit `978935b`. `make check` (coordinator, worktree at the commit): 7009 passed, 3 skipped, 1 xfailed.
- Deviations: none (byte-identical). Reviewer: APPROVED, round 1.
- Notes for the owner (no action taken): (1) the task's Interfaces line says `kind_of` "returns one of `KINDS`",
  but the plan never defines `KINDS` and nothing imports it — left out. (2) RFC §5.1 lists `*.key` as secret-shaped
  and also iWork `.key` as kind_not_admitted; the plan's code settles it by mime (Keynote mime → not SECRET_NAME).
  Not in the Δ table; the reviewer suggests a Δ row for the record.

### Task 4 — done
- Commit `5ee9931`. `make check` (coordinator, worktree at the commit): 7028 passed, 3 skipped, 1 xfailed.
- Deviations: code byte-identical. `src/assets/geo/README.md` written by the implementer from the plan's points (the
  plan gives no text); the reviewer checked its facts against the code and the table.
- **Skipped command (by rule):** `scripts/geo/build_place_table.py` was not run; `places_v1.tsv` untouched. Read-only
  check through `load_place_rows`/`PlaceIndex`: 64 458 rows; Trastevere → Rome (Roma), central Porto → Porto,
  Brooklyn → New York, Dolomites → Brixen (Bressanone), open sea → none.
- Reviewer: APPROVED, round 1 (mutation-tested 6 breaks, each caught by one test).

### Task 5 — done
- Commit `186c653`. `make check` (coordinator, worktree at the commit): 7033 passed, 3 skipped, 1 xfailed.
- Deviations: none. Reviewer: APPROVED, round 1.
- Note: the plan's `DriveResyncRequired` docstring in `src/domain/user_drive.py` says "(Graph 410)" — a provider name
  outside the allowed files, docstring only. Kept verbatim; owner may neutralise it ("cursor expired, HTTP 410").

### Task 6 — done
- Commit `b91aab2`. `make check` (coordinator, worktree at the commit): 7095 passed, 3 skipped, 1 xfailed.
- Deviations: none (byte-identical). Reviewer: APPROVED, round 1.
- Note: the task's Interfaces line lists `ObservationPlan(..., reobserve)` but the plan's code has no `reobserve`
  field (consistent with Δ18; nothing reads it). Stale wording in the plan only.

### Task 7 — done
- Commit `50f534b`. `make check` (coordinator, worktree at the commit): 7098 passed, 3 skipped, 1 xfailed.
- Deviations: none (byte-identical). Reviewer: APPROVED, round 1.

### Task 8 — done
- Commit `57414b7`. `make check` (coordinator, worktree at the commit): 7109 passed, 3 skipped, 1 xfailed.
- Deviations: none (byte-identical). Reviewer: APPROVED, round 1.
- Reviewer notes for Part C parity: in-memory `lowest_ranked_carded` sorts a missing `modified_at` as newest
  (`inf`) while `card_capped` sorts it as oldest (`0.0`); `filter_records` drops undated records even without a date
  bound; `find_by_name_keys` does not enforce the 30-key cap (callers do, Δ17).

### Task 9 — done
- Commit `048a3e0`. `make check` (coordinator, worktree at the commit): 7149 passed, 3 skipped, 1 xfailed.
- Deviations: none (all four blocks byte-identical). Reviewer: APPROVED, round 1 (mutation-tested).
- **OWNER — two tick paths without a test** (the plan's verbatim code is correct, but nothing pins it; Task 10's
  property suite also passes under both mutations):
  1. per-page `drive_id` mismatch → `_DriveSwitched` → purge (§7.1 step 1): replacing the `raise` with `pass` keeps
     all Task 9 + Task 10 tests green. `test_drive_switch_purges_then_onboards` only covers the connection-level switch.
  2. the Δ15 round gate on work-off (`if state.round_complete:` → `if True:`) also stays green; today the time budget
     masks it. Worth two new tests as a follow-up (not added: beyond the task's scope).
- Plan-code notes: `except DriveItemNotFoundError:` in the re-listing path does not log (Global Constraint "every
  except logs"); `TickResult` is a second public class in the service file (part of the plan's interface).

### Task 10 — done
- Commit `b677cf7`. `make check` (coordinator, worktree at the commit): 7409 passed, 3 skipped, 1 xfailed.
- Deviations: none (byte-identical). Step 2's manual 1000-seed run: 1060 passed (reverted to 200 seeds, uncommitted).
- Reviewer: APPROVED, round 1 (mutations in the policy and planner caught: 99 and 21 failures).
- Note: the random pool never yields Δ16 generated dirs or dense trees; the Part F hostile-tree test covers them.

### Task 11 — done
- Commit `8da04bb`. `make check` (coordinator, worktree at the commit): 7414 passed, 3 skipped, 1 xfailed.
- Deviations: none. Reviewer: APPROVED, round 1.

### Task 12 — done
- Commit `b1402fb`. `make check` (coordinator, worktree at the commit): 7418 passed, 3 skipped, 1 xfailed.
- Deviations: none. Reviewer: APPROVED, round 1. The existing `test_callback_saves_credentials_under_drive_provider`
  now passes through the best-effort path and logs one harmless "Drive id not read at connect" WARNING.

### Task 13 — done (with skipped commands, by rule)
- Commit `e394b4f` (sanitiser script + guard test, byte-identical to the plan). `make check` (coordinator, worktree
  at the commit): 7444 passed, 3 skipped, 1 xfailed.
- **Skipped (raw S0a reports with PII exist only on the owner's machine; fixtures pre-committed in `a9e8a24`):**
  - Step 4: `venv/bin/python scripts/onedrive/sanitize_delta_fixtures.py --source ../alekbot-core/scripts/memory`
  - Step 6 (determinism): the same command twice + `git diff --exit-code tests/fixtures/onedrive_delta`
  - Step 7: fixtures not re-added (already tracked, unchanged).
  The sanitiser was run only with `--help`. Step 5's read-only PII greps on the committed fixtures: all clean (the
  `git config user.name` grep is meaningless here — not the owner's name in this container).
- Step 2 note: 24 of 26 tests already passed before the script existed (they read the pre-committed fixtures).
- Reviewer: APPROVED, round 1. **OWNER:** the determinism check (Step 6) can only be run on your machine.

### Task 14 — done
- Commit `51a7cbc`. `make check` (coordinator, worktree at the commit): 7497 passed, 3 skipped, 1 xfailed.
- Deviations: none (every block verbatim, placed where the plan says). `make check-types` informational count
  unchanged (61, none in the touched files).
- Reviewer: APPROVED, round 1 (20 adapter mutations, 19 caught; removing `"audio"` from `_FACETS` survives — no
  fixture has audio, per ΔB4).
- Log ordering note: the run-log commit titled "Task 13" lands after `51a7cbc` because log entries are committed
  only while no implementer runs (avoids `.git/index.lock` races).

### Task 15 — done
- Commits `deba493` (plan-marked reviewer-approved edit of three tests in `tests/unit/adapters/test_onedrive_adapter.py`,
  review_B; applied verbatim, reviewer confirmed byte-identical via `patch` + `cmp`) and `7b15be6` (implementation).
- `make check` (coordinator, worktree at `7b15be6`): 7508 passed, 3 skipped, 1 xfailed.
- Step 4 before the test edit: exactly the three pre-approved tests failed (DriveAccessDenied / DriveThrottled /
  DriveAccessDenied); no other existing test failed.
- Reviewer: APPROVED, round 1. Part B complete.

### Task 16 — done
- Commit `a9ba722`. `make check` (coordinator, worktree at the commit): 7511 passed, 3 skipped, 1 xfailed.
- Deviations: none. Reviewer: APPROVED, round 1.

### Task 17 — done
- Commit `45c5795`. `make check` (coordinator, worktree at the commit): 7578 passed, 3 skipped, 1 xfailed.
- Deviations: none (four files byte-identical). Reviewer: APPROVED, round 1 (7 mutations, all caught).
- Note: `records_seen_before` orders by (seen_epoch, item_id), the in-memory one by item_id (port leaves order open).

## Review method change (after Task 17, on the user's request)

Reviews of Tasks 0–17 checked "commit == plan" — with verbatim copying that is a byte diff, and the reviewer prompt
told reviewers to treat the plan's code as the standard, so real findings were filed as notes. From Task 18 on the
reviewer prompt adds a mandatory **adversarial pass over the code itself** (RFC divergence not covered by a Δ, logic
bugs, races/redelivery, Firestore limits, logs/PII, mutation testing) and classifies findings: **IMPL** (fix),
**PLAN-BUG with a local fix** (fix + a NEW failing-first test; existing tests untouched), **OWNER** (design decision,
logged here). Tasks 0–17 get a retrospective adversarial review in three read-only areas (domain, tick, drive +
storage); results and the fixes they lead to are logged below.

### Task 18 — done
- Commit `b892ab8`. `make check` (coordinator, worktree at the commit): 7583 passed, 3 skipped, 1 xfailed.
- Deviations: none (plan diff applied on base blob 29a7868; result byte-identical). `make deploy-indexes` NOT run.
- Reviewer (adversarial): APPROVED, round 1, no findings. gcloud flag syntax checked against local `--help`;
  11 mutations, 2 survivors with no behaviour change. Pre-existing observation: the script lowercases scope to
  `collection_group` (gcloud wants `collection-group`) — no current index uses that scope.

### Task 19 — done
- Commit `34f163c`. `make check` (coordinator, worktree at the commit): 7586 passed, 3 skipped, 1 xfailed.
- Deviations: none. Reviewer (adversarial): APPROVED, round 1. Every repository query, incl. Part E's search and
  Task 22's `oldest_due_at`, is backed by a declared index; vector entries 768-dim, matching the Gemini adapter.
- F1 MINOR (test gap, not required): `_required_index` treats a range filter without `order_by` as needing no
  composite index; a future such query would pass the test and fail in production.
- **OWNER (before Task 47):** `make deploy-indexes` creates every entry in `config/firestore.indexes.json`, incl.
  ~25 older entries for the unprefixed collections deleted on 2026-05-31; Δ22 says those only burn quota. Decide
  whether to prune them from the file before the deploy.

## Second review change (after Task 19, on the user's request)

The reviewer prompt now also has a **system-context pass**: read the live code around the diff (callers, port
implementations, Firestore fields, task types, DI, dedup, OIDC, notifications, deploy substitutions), look for
regressions of Step 1 drive / To Do / Gmail and startup, and **challenge the plan's premises** (each Δ and design
decision the task relies on, checked against code, RFC and spike data). A separate read-only **premise audit** of the
whole plan (Δ1–Δ22, part decisions, integration assumptions of Tasks 22–46) runs in parallel.

### Task 20 — done
- Commit `d2f94bb`. Script written, checked only with a stub `gcloud` (never run). `make check` (reviewer, at HEAD
  with the commit): 7586 passed, 3 skipped, 1 xfailed.
- Reviewer (adversarial + system context): APPROVED, round 1. MINOR plan notes: the script exits 0 when both creates
  fail (`|| echo`, no `-e`) — caught later by Task 21's smoke and Task 45/47's READY check; no test ties the script to
  the JSON's `card_vector` entries.

## Retrospective adversarial review of Tasks 0–17 — results

Reports (three areas, read-only, mutation-tested in throwaway worktrees): domain 1 BLOCKER / 6 MAJOR / 8 MINOR;
tick 0 / 5 / 3; drive + storage 0 / 1 / 5. Full reports are summarised below; fixes run as review-driven fix tasks
R1–R5 (implementer → adversarial reviewer → acceptance), each with NEW failing-first tests.

| Fix | Area | Findings fixed |
|---|---|---|
| R1 | planner/domain | domain F1 BLOCKER (deleted repo resurrected when its tombstone precedes `.git`'s), F3 (density anchor survives clean-and-refill → never re-probed), F10 (`seen_epoch` re-stamp lets a gone record survive a sweep), F13 (offset-less takenDateTime → naive date), F14 (place grid misses > 63° lat) |
| R2 | tick | tick F1 (systemic error handlers overwrite the cursor/epoch saved by observe), F2 (systemic error inside inline probe dropped as one bad item), F3 (nudged items not class 0, RFC §7.8), F6 (continuation loop); tests for the per-page drive switch and probe parking |
| R3 | adapter | drive F1 (cursor not tied to drive/App Folder → stuck forever after account switch or re-created folder), F3 (token timeout charged to an item), F6 (test gap), tick F2 adapter part (`aiohttp.ClientError`) |
| R4 | repositories | drive F2 (`save_sync_state` writes the lease back; a stale tick releases the new holder's lease) |
| R5 | eval fixture | domain F6 / Task 0 note: Porto latitudes |

**Owner decisions from the retrospective (not fixed overnight):**
1. **Card cap erases the structural reason** (domain F2 = tick F4, MAJOR, reproduced): `capped()` overwrites `reason`
   with `USER_CARD_CAP`; a capped repo is re-opened and re-listed on every re-emission, a capped repo whose `.git` is
   deleted stays opaque forever, a released dense folder gets the "repository" card. Options: a separate cap flag;
   never evict folder cards; derive the structural reason from scope.
2. **Place names** (domain F4, MAJOR): on the bundled table 6 411 of 18 753 towns photographed at their own centre get
   another place's name, 116 across a border (Monaco → Nice, Vaduz → Feldkirch, Versailles → "Paris 15 Vaugirard").
   Re-tune Δ7 (radius/weight) and exclude city districts in the builder — needs a table rebuild on your machine.
3. **Embedding space never compared** (domain F5, MAJOR): RFC §12.3's re-embed-on-model-change path is missing, no Δ
   covers it.
4. **Accent folding** (domain F7, MAJOR): "resume" misses `Résumé.pdf`; cheap now, a re-key later.
5. Tokenisation (domain F8: `’ — – %` glue tokens), Ukrainian transliteration (F9), generated-dir cards labelled
   "repository" (F11), dense-tree card text re-embedded every tick because it contains the live child count (F12).
6. **A delta link answering 404** (tick F5): if the App Folder is deleted and Graph answers 404 rather than 410, every
   tick fails; R3's root-id check covers the re-created-folder case, the plain 404 needs a spike.
7. A policy bump re-lists every repository once (tick F7) — converges; accept or tie the marker to its own version.
8. Token endpoint: every non-`invalid_grant` refresh failure becomes `DriveThrottled(60)` (drive F4) — an expired
   client secret means a tick per minute per user forever; `graph_auth` is shared with To Do, so not changed overnight.
9. `flag_reconnect` read-modify-write can restore an old grant if the user reconnects in between (drive F5): needs a
   field-level conditional write on `OAuthCredentialsPort`, or accept and correct Task 14's text.
10. Fencing a stale tick's other writes (drive F2 OWNER part).

### Fix R1 (planner/domain) — done
- Commits `ab1d18b` (source fixes + new failing-first tests) and `0124398` (round 2: two test gaps the reviewer found).
  `make check` (reviewer, at `0124398`): 7605 passed, 3 skipped, 1 xfailed.
- domain F1 BLOCKER fixed (`_marker_left` ignores a `DELETING` old parent; planner + tick tests, both page orders).
  F3: new `Classification.density_reset` flag (justified deviation: `density_count=0` already means "keep anchor").
  F10: keyword-only `plan_observation(..., observed=True)`; the five re-plan sites pass `observed=False`.
  F13: offset-less timestamps read as UTC. F14: `_lon_cells` widens the longitude scan above ~63°; below 62.5°
  results are unchanged. `GEO_TABLE_VERSION` not bumped (nothing stored yet).
- Review round 1: CHANGES REQUIRED (upgrade/release re-plan paths unpinned; one vacuous test). Round 2: APPROVED,
  12/12 mutants killed. No existing test failed or was edited.

## Premise and integration audit of the whole plan — results

No BLOCKER. Everything Tasks 20–46 assume about existing code (names, signatures, env vars, collections, deploy
order, substitution handling, OIDC audience, `main.py` names) matches the code. Findings and their handling:
- **P1 MAJOR → fix after Task 31:** Firestore `array-contains-any` returns the first 100 matches by id, so all
  trigram/translit keys in one capped query make "contarto" / "leese agrement" / "Догов" return the 100 oldest
  trigram-sharing files, not the target. Gold set cannot see it (every fuzzy case keeps one exact token).
- **P2, P8 (tick part), P9 → fix R2b** on the committed tick; **P8 adapter part → R3**.
- **P4** (empty query + no filters → silent "8 newest files") → fix after Task 33; **P5** (`/api/drive/status` 500s
  when indexing status fails, hiding Connect/Reconnect — a Step 1 regression) → fix after Task 34; **P7** (Quart
  cancels the fan-out request at scheduler disconnect, a delivered reconnect notice can repeat) → fix after Task 22.
- **OWNER:** P3 (escaping `& < >` in opened drive text: a "fix and save" can write entities back into the file),
  P6 (nudges are sent for users with indexing off, waking the indexer; RFC §7.1 says nothing is enqueued for them),
  P14 (date filters cut at UTC midnight, not the user's timezone).
- **Task 47 notes:** P10 (alert policies are scoped to `alek-bot-dev`; verify they cover the indexer), P11 (add a
  probe `drive_sync` task to prove Cloud Tasks passes internal ingress), P12 (Step 3 expects a WARNING Task 24 logs
  at INFO), P13 (Δ11 says Task 21, it is Task 33).

### Fix R2 (tick) — done
- Commits `24ad1e4` (round 1) and `79a53f8` (round 2). `make check` (reviewer, at `79a53f8`): 7623 passed, 3 skipped,
  1 xfailed. `24ad1e4` alone was red (3 existing tests) and was pushed only together with `79a53f8`.
- tick F1 fixed (a `_Saved` holder: systemic handlers write onto the latest saved state; incl. right after a 410).
  F2: `_apply` re-raises not-connected / throttled / opt-out; other probe errors → failed probe (`probe_pending`).
  F3: nudged items scheduled class 0. F8: tests for the per-page drive switch and probe budget + parking.
- F6 round 1 broke three existing tests (`test_every_version_bump_completes_and_applies[policy|geo_table]`,
  `test_version_bump_restamps_and_applies_after_clean_pass`). **Reviewer decision: the tests are right** (they pin
  plan-intended in-chain verification after a version bump), the fix was wrong; tests untouched. Round 2: continue
  on a version difference while a pass is mid-way **or** this tick's upgrade wrote anything; `_upgrade_page` skips
  parked `UNRESOLVED` records with no due time (the remaining spin: 30 back-to-back ticks → 2, then converges).
- Review: round 1 CHANGES REQUIRED, round 2 APPROVED (mutations killed; one equivalent survivor).

### Fix R2b (tick, from the premise audit) — done
- Commits `9b799d9` (round 1), `4998508` (reviewer-approved narrowing of the implementer's own new P9 test to the
  400 case), `f248047` (round 2). `make check` (reviewer, at `f248047`): 7638 passed, 3 skipped, 1 xfailed.
- P2: the opt-out branch enqueues a continuation; the next tick's gate purges. P8: `_Budget.item_call_allowed()` (item
  calls left **and** time left) at all four item-call sites. P9: card embedding gathers per record; **only**
  `LLMClientError(http_status=400)` backs one card off; everything else (429/503/5xx/other 4xx/network/unknown) stays
  systemic per RFC §8.2.
- Round 1 had it backwards (treated 5xx/403/network as per-record → `attempts` climbed, tick "done", staleness alert
  silent); caught by the reviewer reading the real adapter. Round 2 also maps non-429 `genai` `ClientError` →
  `LLMClientError(code)` in `GeminiEmbeddingAdapter` (wire tests at the SDK boundary; all other embedding callers
  catch `Exception` or propagate — unchanged). Docs: embedding_system README §2.3, `src/adapters/CLAUDE.md`.
- Accepted residual: a configuration 400 (invalid API key) backs cards off one by one (non-terminal; memory search
  breaks at the same time). **OWNER/doc:** RFC §8.2 has no explicit row for a card refused with a 400.

### Fix R3 (OneDrive adapter) — done
- Commits `dcf9ab3` (round 1), `754a3a1` (reviewer edit of two pre-existing tests), `ce483bb` + `2c86ef9` (round 2).
  `make check` (reviewer, at `2c86ef9`): 7664 passed, 3 skipped, 1 xfailed. `dcf9ab3` alone was red (2 tests) and was
  pushed only with `754a3a1`.
- drive F1: page cursors carry `drive_id` + `root_id`; a mismatch with the fresh App Folder read raises
  `DriveResyncRequired` before the old URL is followed (old cursors without the keys still work). **Verified end to
  end** (real adapter + real tick, connect-time `drive_id` read failed, A's deltaLink answering 400): purge, then a
  clean onboarding of B; the parent commit looped on `error`. drive F3: token-endpoint timeout → `DriveThrottled`.
  drive F6: test. Per-call timeout `GRAPH_CALL_TIMEOUT_S = 60` on `_call`.
- **Reviewer decision on existing tests** (`tests/unit/adapters/test_onedrive_delta_wire.py`): both encoded the old
  cursor format that F1 changes on purpose → edited by the reviewer (`754a3a1`): the exact-equality check now includes
  the binding (stricter); the fresh-approot test uses an unbound cursor, all assertions kept.
- Round 1 mapped every transport error/timeout to `DriveThrottled` — **coordinator brief error** (I called it "the
  systemic class the report proposes"). RFC §8.2 classifies by the call: item calls are per-item. Round 2: timeouts
  stay `TimeoutError`, other `aiohttp.ClientError` → `OSError` (both per-item in the tick); the feed backs off
  exponentially via the generic handler; the token endpoint stays systemic. Step 1 answers "Drive operation failed:
  OSError/TimeoutError." (never "not connected" / "still running").
- Notes: a feed network failure shows `last_error=INTERNAL` (as before R3; changing it alters Task 36's Cabinet text);
  download/upload chunk requests and the `graph_auth` token POST keep aiohttp's 300 s default (pre-existing); a 410
  restart cursor carries no binding (narrow gap, accepted).

### Fix R4 (lease) — done
- Commits `a2ddf04` (fix) and `9ec49f9` (reviewer-approved edit of one pre-existing test). `make check` (reviewer):
  7671 passed, 3 skipped, 1 xfailed.
- `save_sync_state` no longer writes `lease_owner`/`lease_until` in either repository (`STATE_FIELDS` excludes them;
  the create path of `acquire_lease` writes them; in-memory keeps the stored lease). A stale tick can no longer
  release the new holder's lease. Create / take / renew / release / purge paths verified.
- **Reviewer decision on an existing test:** `test_firestore_drive_index_repo_serialization.py::TestStateSerialization
  ::test_doc_excludes_fanout_flags` asserted `set(state_to_doc(...)) == set(STATE_FIELDS)`, i.e. that what is
  serialised equals what a save writes; F2 separates them on purpose. Edited to two exact (stronger) pins.
- OWNER part (fencing a stale tick's other writes) not done.

### Fix R5 (eval fixture, Porto) — done
- Commit `f1e2b9c`. `make check` (reviewer): 7672 passed, 3 skipped, 1 xfailed. The Task 0 OWNER item above is
  **resolved**: `IMG_6202..6206` latitudes → 41.1585..41.1609 (+0.0006 step), longitudes and `gold_g1.json` untouched.
  New test: every `Photos/Porto/*` photo resolves to "Porto, Portugal" (failed on the parent). All six cards now carry
  "place Porto, Portugal". The plan file (lines ~306–310) still shows the old latitudes — left as is (plan text).
- `place-porto-ru` ("фото из Порту") still depends on search matching Cyrillic "Порту" to "Porto".

### Fix R6 (pinning tests, retro domain F15 + tick F8 c–j) — done
- Commits `4109ab1` (22 new tests in two new files, no `src/` change) and `2901f5c` (round 2: the reviewer's exact
  one-line change, applied by the coordinator — the maintenance-share test pinned `_DUE_PAGE`; the reviewer verified
  the new assertion passes on HEAD and with `_DUE_PAGE = 64` and still kills M22).
- All 20 named mutants killed, each by its own test; **no test failed on HEAD → no further bug found**.
  `make check` (reviewer, worktree at `4109ab1`): 7694 passed, 3 skipped, 1 xfailed.
- Left open (INFO): a partial M7 mutant (dropping only the `card_eligible` half of the `_embed_cards` re-check)
  survives; a record made `INSIDE_OPAQUE` by a rescope in the same batch would pin it.

### Environment limits (logged by rule, not worked around)
- **No GCP credentials:** `GOOGLE_APPLICATION_CREDENTIALS=/opt/gcp-sa.json` points to a missing file,
  `gcloud auth list` shows no account, no `.env`. → Task 21's live read-only smoke run was not executed.
- **No Gemini API key** in the environment → the optional real-embedding eval after Task 41 cannot be run.

### Task 21 — done (live run skipped: no credentials)
- Commit `819eea8` (script + test, byte-identical to the plan; no adaptation needed after R1–R6). `make check`
  (reviewer, worktree at the commit): 7696 passed, 3 skipped, 1 xfailed.
- Script verified read-only (no write method, no batch/transaction/set/update/delete; the test asserts it). Every
  query it runs has a declared index. **Skipped:** the live run (see Environment limits) → Task 47 Step 2.
- Reviewer (adversarial): APPROVED. MINOR plan test gaps, not required: tests only count queries (dropping the `kind`
  argument from the three kind checks survives); no test on the CLI exit code (Task 47 gates on "exits 0").

### Task 22 — done (fix R7 queued)
- Commit `bb781b0` (byte-identical; no adaptation needed). `make check` (reviewer, worktree): 7724 passed, 3 skipped,
  1 xfailed. Reviewer (adversarial + system context): APPROVED, nothing BLOCKER/MAJOR.
- Fix R7 queued: F1 (= premise audit P7, confirmed: Quart 0.23.1 cancels the request at the scheduler's 180 s
  disconnect, and SIGTERM cancels in-flight requests; a notice cut off after sending → one duplicate on the next run)
  → set the flag before `notify`, roll back only when nothing was sent (Gmail precedent `_flag_gmail_reconnect`);
  F2 (test: a deliberate disconnect with the flag down sends no notice — mutation M8 survives today).
- **OWNER (F3):** the plan's premise "a formatter failure may have reached the user" is false when the agent returns
  non-success (`notify` returns before sending), so one transient Quick failure means no reconnect notice for that
  breakage. Accept at-most-once (as Gmail does) or add a "sent anything" field to `NotifyResult`.

### Task 23 — done
- Commit `001a45a` (byte-identical). `make check` (reviewer, worktree): 7738 passed, 3 skipped, 1 xfailed.
  Reviewer (adversarial + system context): APPROVED, no required fixes; 10/16 mutations killed, 6 harmless survivors.
- **OWNER (premise audit P6, confirmed as a fact):** nudges go to users with indexing off. Cost per bot write: one
  Cloud Tasks create, maybe an indexer cold start, one slot on the concurrency-1 queue; the tick answers `disabled`
  before the lease and writes nothing (and if an orphaned state exists, the nudge purges it sooner). The reviewer reads
  RFC §7.1's "nothing is enqueued for a user with indexing off" as about the tick's own follow-ups → ambiguity, not a
  clear divergence. No local fix (the service has no access to the flag). Options: (a) accept and reword §7.1 step 0
  (reviewer's recommendation); (b) inject `UserRepository` or a `should_nudge` predicate (one Firestore read per
  mutation); (c) the agent-cached flag (not recommended, up to ~1 h stale).

### Fix R7 (reconnect notice) — done
- Commit `6943931`. `make check` (reviewer, worktree): 7751 passed, 3 skipped, 1 xfailed. Reviewer: APPROVED, 8/8
  mutations killed. The flag is written before `notify`; rolled back only when nothing was sent or `notify` raised
  (re-checked: `notify` raises only before any send); a `CancelledError` keeps it up → a cut-off notice is never
  repeated but may be lost (at-most-once, as the Gmail path). Docs tasks 44/46 should say so.

### Task 24 — done
- Commit `200ae43` (byte-identical; no adaptation needed). `make check` (reviewer, worktree): 7748 passed, 3 skipped,
  1 xfailed. Reviewer (adversarial + system context): APPROVED. Boot checked: `DRIVE_INDEXER_URL` unset → no queue,
  no I/O, nothing new can crash startup; Step 1 drive unchanged without a queue; `os.getenv` vs `config.get` correct.
- MINOR test gaps (queued for a small test batch): the scheduler builder's notifier/alert-sink pass-through is
  untested (dropping either survives → the reconnect notice and staleness alert would silently never fire); the
  container repository type is untested.
- Task 47 note (= premise audit P12): Step 3 expects a WARNING, but "drive queue disabled" is INFO (INFO is right).

### Task 25 — done (fix R8 queued)
- Commit `88a0484` (byte-identical). `make check` (reviewer, worktree): 7754 passed, 3 skipped, 1 xfailed.
- Reviewer (system context) found a **MAJOR plan bug** (reproduced with the real coordinator, registry and
  notification service): the reconnect notice is lost whenever the user's agents are not loaded in the main process
  (cold start, or idle eviction after ~1 h). `_reconnect_notice` calls `notify` without `ensure_agents_for_user`
  (every other notifying `/worker` path loads agents first); the Quick formatter is unknown → FAILED, the flag stays up,
  the user is never told. → fix R8 (inject `ensure_agents`, called before the flag). The same latent bug exists in
  the Gmail daily review (`_flag_gmail_reconnect` notifies before `ensure_agents_for_user`) — outside this plan,
  queued as a separate suggested task.
- MINOR: Task 45 doc says the unconfigured fan-out "logs a WARNING" (it is INFO, correctly); `main.py` wiring has no
  test (Task 47 live acceptance covers it).

### Task 26 — done
- Commit `59ab0a1` (byte-identical). `make check` (reviewer, worktree): 7780 passed, 3 skipped, 1 xfailed.
- Reviewer (security focus): APPROVED, no findings. Checked against google-auth 2.61.0: signature/issuer/expiry still
  verified with `audience=None`; the pin rejects a missing or list `aud`; main's `/worker` and the voice verifier stay
  unpinned (existing tests still assert it); Cloud Tasks with no audience → `aud == {DRIVE_INDEXER_URL}/worker`, and
  both sides normalise the same variable; fail-closed on Cloud Run via `K_SERVICE`. 15/17 mutations killed, 2 harmless.
- Operational note for Task 47: a manual `create-http-task` with a different run.app URL form than `INDEXER_URL_DEV`
  gets 401.

### Fix R8 (reconnect notice needs loaded agents; builder/container test gaps) — done
- Commit `9f6e402`. `make check` (reviewer, worktree): 7790 passed, 3 skipped, 1 xfailed. Reviewer: APPROVED.
- `ensure_agents` (keyword-only) on `DriveSyncSchedulerService` and `build_drive_sync_scheduler`, awaited after the
  notifier guard and before the flag; `main.py` passes `agent_factory.ensure_agents_for_user`. **Verified end to end**
  with the real coordinator, registry, notification service and factory load/evict: old code → "Unknown recipient",
  0 notices, flag stuck; fixed → delivered on cold start and after idle eviction.
- Optional gap (N1): no test pins that agents are loaded only when a notice is due (moving the call to the top would
  build every enabled user's agents every 10 min); the current code is correct.

### Task 28 — committed `7f1d557` (provisioning, files only; review running)

### Task 27 — done
- Commit `ae68478` (byte-identical). `make check` (reviewer, worktree): 7785 passed, 3 skipped, 1 xfailed.
  Reviewer (system context): APPROVED. Boot traced against `main.py`/`relay_main.py`/`job_main.py`: every env read is
  provided by Task 28's container; `places_v1.tsv` reaches the image; `build_app()` 0.4 s / ~156 MB. No telemetry
  (as the relay; the indexer makes no LLM calls → ticks visible in Cloud Logging only, not Logfire).
- **Skipped (environment):** Step 5 local startup smoke — no default GCP credentials.
- **OWNER (F1, latent):** the docstring's "Hypercorn waits 8 s, then cancels the tick; its `finally` releases the
  lease" holds on Python 3.11 (the current `python:3.11-slim` image), and only by accident (`asyncio.run` cancelling
  leftovers). On Python ≥ 3.12.1 Hypercorn waits for connections to close and the graceful timeout never applies
  (probe: `serve` still running at 15 s) → after a base-image bump a deploy mid-tick is SIGKILLed and the lease stays
  held ~31 min (ticks answer `busy`; no data loss). Fix: run `serve` as a task, bound the wait, cancel and await.
- MINOR test gaps (queued with the next small test batch): database name, place lookup wiring, first-deploy log level.
- Out of scope note: Graph token refresh now runs in two processes; a refresh in flight during a disconnect can
  re-save a deleted credentials document (Step 1 `graph_auth.refresh` race).
- Reviewer (system context + security): **APPROVED**. All three diffs byte-identical; YAML 12 steps in the planned
  order; `bash -n` clean; every gcloud flag confirmed with `--help` (SDK 570); `make check` 7790 passed. Ingress
  `internal` + `allUsers` invoker + in-app OIDC judged sound (no widening vs IAM gating); that Cloud Tasks counts as
  internal could not be re-verified offline → Task 47's live checks. Queue/scheduler/resource settings consistent with
  the tick deadline and the lease. Nothing deployed or changed in GCP.
- **OWNER:** F1 — indexer deploys before main, so an indexer startup failure blocks main hotfixes (the 503 this avoids
  exists only on the first deploy's second pass); F2 — a later `make deploy` from a checkout whose `.env` lacks
  `INDEXER_URL_DEV` silently disables indexing on both services and, since the staleness alert lives in the fan-out,
  nothing alerts (optional Makefile guard); F6 — Slice 3's main → indexer `/extract` call will be refused by internal
  ingress (main has no VPC egress).
- Docs (passed to Task 45): `SCHEDULERS.md` "60 s for all /worker jobs" is stale and the deadlines table lacks
  `drive_sync` (1800 s); a manual `create-http-task` gets the 600 s default deadline. No test pins the provisioning
  settings (accepted by the plan).

### Task 29 (Part D gate) — run by the coordinator
- `ruff check indexer_main.py`: clean. `grep DRIVE_INDEXER_URL src/config/settings.py`: no match. Provider-name grep in
  `drive_sync_scheduler_service.py` / `indexer_worker_app.py`: no match. `make check`: see below.

### Task 30 — done (local fixes queued with the search fix batch)
- Commit `ac4e134` (byte-identical). `make check` (reviewer, worktree): 7861 passed, 3 skipped, 1 xfailed.
  Reviewer: APPROVED. Wrapper sound (body `& < >`, attributes `& < > " '`, nonce drawn after content; look-alike
  characters cannot forge a close); path derivation, cycle/depth handling and the gate agree with the indexer.
- Queued LOCAL fixes: F2 (= P4) an all-empty request renders "at least N match the filters" when none were given →
  word it as "no name words or filters: the N newest"; F3 while rebuilding a filter-only answer says "0 match" under
  "do not say a file does not exist" → drop the count line then; F5 tests (date-filter bounds/undated, naive datetime
  string, gate guard for a state without `drive_id`).
- **OWNER:** F1 (= P3) opened HTML/Markdown/code reaches the model entity-escaped and can be written back via
  `save_file_to_drive`; also two non-wrapped places escape for no benefit (`R&D's` → `R&amp;D&#x27;s`). If the
  owner picks "escape only the tag", the *opening* `<drive_document` must be escaped too (case-insensitive, with
  whitespace), or a file can forge a nested block. Otherwise keep full escaping + one reminder sentence.
  P4 second half: refuse an empty request or not ("show my newest files" is legitimate). F4: RFC §10.4 says body
  quotes are escaped; the plan escapes only `& < >` — needs a Δ row or an RFC edit. F6: `doc` = `.doc` only, the
  "document" family excludes PDFs ("documents I saved last week" misses PDFs), plural extensions refused.
  F7: date-only bounds are UTC days (= P14).
- Part D gate `make check` (coordinator, clean worktree at `019c069`, i.e. Part D + R7/R8 + Tasks 30–31):
  7921 passed, 3 skipped, 1 xfailed — green. **Part D complete.**

### Task 32 — done
- Commit `dfb59d1`. `make check` (reviewer, worktree): 7932 passed, 3 skipped, 1 xfailed. Reviewer: APPROVED.
- The pre-existing `tests/unit/infrastructure/test_agent_manifest_drive.py` edit is the plan's reviewer-approved one
  (review_E §7.1): `test_no_search_intent_in_step_one` → `test_no_search_text_field`, its four "no search intent"
  assertions dropped (they pinned USER_DRIVE_RFC §7's "no search in step 1", which the indexing RFC §10.4 reverses);
  each is covered by a stronger positive test in the new file. Confirmed exact.
- System context checked: Smart sees the intent, companions' allowlists exclude it; the `drive_*` names collide with
  nothing so their descriptions reach the model; the four allowlisted collisions are live. MINOR: the old test
  module's docstring still says search is unavailable (→ Task 46 docs sweep).

### Task 31 — committed `019c069` (byte-identical); review: CHANGES REQUIRED → fix batch R10
- `make check` (reviewer, worktree): 7921 passed. **rev_31 F1 MAJOR = premise audit P1 confirmed and reproduced**
  (160 trigram-sharing fillers: "leese agrement", "Догов", "contrsto" all miss — `name_kept=0 fused=50
  widened=False`). **F2 MAJOR:** a filter-only folder search capped by the fetch limit says "No matching files or
  folders" — Smart would tell the user the folder is empty. F3 (never guess: a full folder-name page still accepted a
  single match), F4 (subtree walk reads up to 16× `FOLDER_INMEM_CARDS`), F5 test gaps (M3: widening not pinned to
  once — unbounded loop on a full `FETCH_MAX` page). Gate uncached, Δ20 families and RRF + Δ17 tie-break confirmed.
- Reviewer decision on an existing test: `TestScale::test_one_query_per_whole_token_key_plus_one_for_the_rest` pins
  Δ17's single "rest" call that P1 shows to be wrong → reviewer-approved stricter edit, applied with R10.
- **Plan text affected by the P1 fix (owner):** the Δ17 row ("one query for the remaining keys"), Part E notes, and
  Task 44's README line describe the old shape. Remaining limit: the 8 single trigram calls follow query order, so a
  misspelled first token uses slots; a trigram held by > 100 lower-id files still crowds its own page.

### Task 33 — done
- Commit `dcda59d` (byte-identical). `make check` (reviewer, worktree): 7945 passed, 3 skipped, 1 xfailed.
  Reviewer: APPROVED, nothing BLOCKER/MAJOR; 18/21 mutations killed. Step 1 intents untouched.
- Existing test `test_unknown_intent_lists_no_search`: passes, name now stale (it reaches "not available"); reviewer:
  no edit (cosmetic; the rule covers failing tests).
- **OWNER:** F1 (P3 in real code): escaped HTML/code text can be written back; and the vision/video labels are
  escaped although they are not wrapped (`R&amp;D` may be quoted to the user) — drop `escape_text` there whichever P3
  option is chosen. F3: RFC §10.4/D2 "every delivery of file text carries the reminder" — two paths are outside Δ11:
  (a) the coordinator's prefetch resolves `drive:` refs into `file_content` for DocPlanner/PdfGenerator/HtmlPage with
  no wrapper; (b) file names from search go into history `drive_context` as raw JSON (the Step 1 mechanism). Add a Δ
  narrowing D2, or schedule (a). P4: the overnight fix only rewords the all-empty case (R10); refusing it stays open.

### Task 34 — committed `226dffb`; review: CHANGES REQUIRED → fix R11
- `make check` (reviewer, worktree): 7959 passed. Source and new tests byte-identical; the plan-approved edit of
  `tests/unit/web/test_user_cabinet_app_drive.py` confirmed byte-identical to Step 4 (3 hunks, nothing weakened).
- **F1 MAJOR = premise audit P5 confirmed:** any failure of the indexing read (Firestore blip, building index,
  unparsable state during a rolling upgrade) makes `/api/drive/status` 500, and the Cabinet then renders no drive card
  at all — Connect/Disconnect/Reconnect vanish for every drive user (Step 1 regression). → R11 (own try).
  Test gaps F2–F4 (index block while disconnected; unique toggle keys; flag saved before enqueue) → R11.
- **OWNER (F5):** with no indexer (`DRIVE_INDEXER_URL` empty, e.g. the first deploy pass) the toggle is still shown;
  turning it on does nothing and the status says `building` forever. Gate the block on the queue, or accept for the
  owner-only deploy window (R11 only fixes the false log text).

### Task 35 — done
- Commit `8af1a1d` (byte-identical). `make check` (reviewer, worktree): 7964 passed, 3 skipped, 1 xfailed.
  Reviewer (system context): APPROVED. Boot path, per-user build/eviction, Step 1 unchanged without drive/index;
  Review Focus 4 confirmed (flag read uncached through the only `UserRepository` impl; a warm agent still answers
  `disabled` after the toggle); Cabinet gets the same instances; query and card embeddings share model/space.
- Test gap F1 (`users=None` survives → every search would raise) → added to R11. **OWNER (F3):** search is built
  even without the drive queue (local dev, first deploy pass) → "building" forever if indexing is turned on then.

### Fix R10 (search batch: P1, rev_31 F1–F5, rev_30 F2/F3/F5) — done
- Commits `4eaa7ee` (fixes + new tests), `e2d3c34` (rev_31-approved stricter edit of `TestScale::
  test_one_query_per_whole_token_key_plus_one_for_the_rest`), `5e5e8db` (reviewer-approved: `TestRender::
  test_filter_only_says_how_many_matched` pinned the old wording for a request with NO filters — its request now has a
  date filter, assertion unchanged), `9c35922` (round 2). `make check` (reviewer, at `9c35922`): 7995 passed.
- **P1 fixed:** whole-token and trigram keys each get their own single-key query (≤ 8 each, in query order) + one
  remainder call (≤ 30 keys); "leese agrement", "Догов", "contrsto" now found among 160 trigram-sharing fillers.
  Cost: ≤ 18 parallel queries per round (≤ 36 with the one widening), ≈ 19.8k projected reads worst case (~$0.006);
  served by the declared `name_tokens` indexes. A widened round can decode ~17k docs on 1 vCPU → watch the log line.
- Folder-scope honesty: a fetch-capped empty page no longer says "No matching files or folders" (points to
  `list_files_in_drive`); a single *suffix* folder match from a full name page is refused (never guess), a single
  exact path is used; the subtree walk honours `FOLDER_INMEM_CARDS` with one shared budget; widening at most once
  (pinned — unbounded loop otherwise). All-empty request: "No name words or filters were given: the N newest…";
  while building, no "0 match" line.
- Review: round 1 CHANGES REQUIRED (exact path refused; three render guards unpinned), round 2 APPROVED (31 killed,
  2 equivalent/optional survivors).

### Fix R11 (Cabinet status resilience = P5; test gaps from rev_34/35/27) — done
- Commit `9170788`. `make check` (reviewer, worktree): 8005 passed. Reviewer: APPROVED; every new test kills its
  mutation; the two bug tests fail on the parent.
- `/api/drive/status` reads the index block in its own try (ERROR log, block omitted) → Connect/Disconnect/Reconnect
  survive any indexing read failure; Task 36's JS shows only the toggle under `data.indexing && …`.
- Task 47 note: while the `oldest_due_at` index is still building (first deploy), every Cabinet load logs one ERROR
  (may page the Errors-dev policy). NIT: `drive_indexing_set` docstring still promises an enqueue with no queue.

### Task 36 — committed `34aed96` (review running)
### Task 37 — committed `f2a5ff5` (review running)

### Task 38 (Part E gate) — run by the coordinator
- `make check` in a clean worktree at `f2a5ff5` (Parts A–E + all fixes so far): see next line.
  **8018 passed, 3 skipped, 1 xfailed — green.** No failures (the plan's skeleton-era failures are gone with Parts A–D merged).

### Task 37 — done
- Commit `f2a5ff5` (identical to the plan diff). `make check` (reviewer, worktree): 8018 passed. Reviewer: APPROVED,
  no findings; every claim in the line verified true for Slice 1 after the fixes; the label matches the Cabinet.
- **OWNER (optional):** the `drive_query` field description says "words of the file or folder name"; if Smart leaves
  a place ("Rome") out of `drive_query`, "photos from Rome in August" returns every August photo. Adding "or a place"
  to that description would rule it out.

### Task 36 — done
- Commit `34aed96` (byte-identical). `make check` (reviewer, worktree): 8018 passed. Reviewer: APPROVED.
- Checked in headless Chromium over 10 payloads × 4 languages + toggle flows: no XSS (no server string reaches
  `innerHTML`; `last_error` whitelisted), a missing or partial `indexing` block is handled (R11), every backend error
  category localised, toggle revert on failure. All seven RFC §6 disclosure clauses present and correct in meaning in
  en/uk/fr/es. The card's JS has no committed behaviour test (string/key tests only) — accepted by the plan.
- Test gap F1 (two disclosure clauses not pinned by unique fragments) → small test batch R12.
- **OWNER:** F2 — RFC §6 asks for "records per state"; the card shows carded (+ pending/failed when non-zero), no Δ
  covers it, and Task 47 Step 7 expects per-state counts. F3 — after a revoked grant the user turns search off: the
  card still says "Search still answers from the stored index" (false; index purged), and turning search back on shows
  "Indexing in progress…" indefinitely while access is lost → show the reconnect hint only when search is on (wording
  needed, 4 languages). F4 — `queued:false` ignored (first deploy pass: "starts shortly" while nothing runs).
  F5 — "kept for about 30 days" hint persists after the purge (status has no `access_lost_at`).

### Task 40 — done
- Commit `e04f1a2` (byte-identical). `make check` (reviewer, worktree): 8028 passed. Reviewer: APPROVED.
  Hit rule matches Task 0's gold schema; bars pin K, tree and gold hashes (any change → no verdict); regressions are
  strict per scope (no tolerance); nothing writes bars implicitly.
- Small gaps → R12: F1 (no test pins "no tolerance" — a 0.2 tolerance survives), F2 (a case that missed at record time
  and still misses is not "lost"), F3 (a bars file with a missing/empty `recall` map passes at any recall → add a
  mismatch reason).

### Task 39 — done
- Commit `5004252` (byte-identical). `make check` (reviewer, worktree): 8023 passed. Reviewer: APPROVED, no IMPL.
- Fidelity verified empirically: the real adapter's `_to_change` on the Graph-shaped raw of all 3 266 fixture items
  gives the same `DriveChange` as `FixtureDrive` (bar two fields nothing reads); delta/children/`get_change` contracts
  match; the real tick over an S0a-shaped feed (page sizes 200/50) and shuffled orders produced the same 2 491
  records with 0 differences → the eval measures the real tick, not a fixture artefact.
- Gaps → R12: `fs_modified` (generate/explicit/naive), id stability across processes (pin one id), `video` /
  `photo: {}` / size facets. Docstring says "root on every round"; S0a has it on every page (no effect, wording).

### Task 41 — done (fix R13 queued)
- Commit `4094ccc` (byte-identical). `make check` (reviewer, worktree): 8046 passed. Reviewer: APPROVED.
- → R13: F1 the runner builds requests with its own parser instead of production's `request_from_fields` (an unknown
  kind surfaces only after indexing ≈ 2 500 Gemini calls; `date_from > date_to` would be recorded as a bar;
  `folder: "/"` disagrees); F2 throttled ticks sleep a fixed 30 s ignoring the service's backoff, no cap (Gemini 429s
  could burn quota for hours); F3 test gaps (`also_at`, `k = 0`, `status == "ok"`, `MAX_ERRORS`).

## Eval (offline, informational — NOT the real run)
No Gemini key here, so the real-embedding eval (Task 47 Step 1) was not run. The Task 41 reviewer ran the real
`tree.json` + `gold_g1.json` through `run_eval` with the tests' fake embeddings and the real place table, into a
scratch path (nothing recorded or committed): index settled in 1 tick, 2 481 cards, 0 errors.
- **Recall@8 32/37 = 0.865.** 100 %: exact 6/6 (both `k:1` cases first: `exact-img4912`, `exact-img0417`), partial
  5/5, translit 4/4, misspelled 4/4, kind 3/3, folder 4/4, filter_only 3/3, version_family 1/1. date 2/3, place 0/4.
- Repeated with 6 salted hashing embedders: all 32 hits held every time → they come from the name stream and filters.
- **Misses that look like real bugs: none.** Misses that need real embeddings: the four `place-*` cases (the place is
  only in the card text). `date-rome-august` may miss **even with Gemini**: the date filter runs after the vector
  search, whose pool is capped at 1 000 of ~2 481 cards and ~1 030 photo cards compete (RFC §10.1 filters after the
  cut) — **OWNER**: accept as a recorded limit or adjust the case, after the real run.
- Note: for "ред3" the top result is the Office lock file `~$Договор_ред3.docx` (carded per RFC §5.1); the expected
  file is still in the top 8 — **OWNER** may want lock files excluded from cards.

### Task 42 — done
- Commit `80905eb` (byte-identical). `make check` (reviewer, worktree): 8083 passed. Reviewer: APPROVED.
  Every (scope, reason) row checked against RFC §5.1/§5.2/§5.5/§3.4 and Δ16 (not against the code): all correct.
  Real tick end to end (3 267 observations, 2 481 cards, 0 errors; no embedding inside an opaque root); late `.git`
  converges to one new embedding also with the parent re-emitted, nested folders, and a card cap of 10. 12 targeted
  policy/planner mutations killed by this file alone.
- MINOR: the "nothing inside an opaque root is carded" check reads the record's own scope only (a one-level re-scope
  mutation survives here but 22 property/tick tests catch it) — optional ancestor-walk test (code in the review).
  **OWNER (= retro domain F11):** Δ16 roots are carded "`node_modules | repository`" / "`__pycache__ | repository`",
  so "my repositories" matches them; a "generated files" label needs a `CARD_VERSION` bump.

### Fix R12 (small batch: disclosure fragments, eval metrics, fixture drive) — done
- Commit `29c3b0d`. `make check` (reviewer, worktree): 8098 passed. Reviewer: APPROVED. Code fix: `bar_mismatch`
  reports a reason when the bars' `recall` lacks `overall` (no verdict instead of a pass). The rest are new tests
  killing the named mutations. NIT for docs: `drive_eval/__main__.py` exit-2 docstring omits this cause.

### Fix R13 (eval runner) — done
- Commit `34318ca`. `make check` (reviewer, worktree): 8113 passed. Reviewer: APPROVED, 15/15 mutants killed.
  Requests are built by production's `request_from_fields` and validated before indexing; throttled ticks sleep
  `max(pause, queued delay)`, stop after `MAX_THROTTLED = 6` consecutive throttled ticks (≈ 31 min of backoff — the
  code comment's "about an hour" is corrected in the docs batch). Offline eval re-run at `34318ca`: identical 32/37,
  same per-case results.

### Tasks 43 / 44 (docs) — fix rounds running
- Task 43 `cd71362` → review: CHANGES REQUIRED (the records repeated RFC §7.1's "lease = start + deadline + margin, so a
  holder never outlives it", which R4 and the retrospective disproved; thumbnail triage attributed to Slice 2, RFC §5.3
  says Slice 4) → fixed in `94cb243`, re-review running.
- Task 44 `86a097b` → review: CHANGES REQUIRED. **New OWNER item from it:** a stale tick that outlives its lease can
  still **purge** (`_DriveSwitched` → `delete_sync_state`), which deletes the whole state document **including the new
  holder's lease**; the holder's next save recreates the state without a lease → a third tick can run alongside. Same
  root as the open "fence a stale tick's other writes" item (R4 OWNER part); RFC §7.1 line ~429 still says "a holder
  never outlives its lease" (RFC not edited overnight).
- **Task 43 — done:** `cd71362` + `94cb243`; round 2 APPROVED, every changed statement verified against the code;
  `make check` 8113 passed. Records are 49–60 lines (CLAUDE.md convention, over the plan's 24–26; accepted).
- Task 45 `04a48f9` committed (review running); its implementer also applied the review notes to `SCHEDULERS.md`.
  Gaps outside its scope noted: `.env.example` lacks an `INDEXER_URL_DEV` placeholder; `SCHEDULERS.md` line 9 says a
  scheduler call "never runs an agent" (the drive fan-out can run the formatter for a reconnect notice).
- **Task 44 — done:** `86a097b` + `5b2b3c0`; round 2 APPROVED (every changed statement verified). One MINOR left for
  the final cleanup: the README's "empty `indexing.counters` = purged" signal is wrong for an index that never
  finished a round (counters are `{}` until a round settles) → use the `index purged` log line only. Also: the module
  docstring of `firestore_drive_index_repo.py` (lines 23–25) omits that `delete_sync_state` removes the lease.
- Task 46 `c9c0098` committed (review running).
- **Task 46 — done:** `c9c0098`; APPROVED (every added claim in both CLAUDE.md files verified; probe edit parses, not
  run). Two MINOR wording fixes queued for the final cleanup ("every file is metadata_only" — reason may be a
  never-download rule; a failed token refresh backs off a flat 60 s, not the provider's delay — same root as owner
  item 8, `graph_auth`). Pre-existing: CLAUDE.md's `WorkerHandler` list lacks `video_generation_polling`.
