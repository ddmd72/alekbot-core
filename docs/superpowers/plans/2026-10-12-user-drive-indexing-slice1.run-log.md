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
