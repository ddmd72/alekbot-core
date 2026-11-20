<!-- living-doc
covers:
  .github/workflows/ci.yml
  .github/workflows/render.yml
  .github/workflows/mutation.yml
  .github/workflows/parity.yml
  .github/workflows/living_docs.yml
  scripts/ci/
  scripts/check_coverage_layers.py
  scripts/check_coverage_thresholds.py
  scripts/quality/
  scripts/seed_testmondata.py
  tests/support/semantic_mutants/
  tests/plugins/
  .coveragerc
  pytest.ini
  ruff.toml
  mypy.ini
  mypy-adapters.ini
-->
# 11 Quality

Quality is enforced by gates that run, not by documents. This doc says what each gate is for and what runs where. What
each `make` target does: root `CLAUDE.md` Commands. Test rules (fakes over call assertions, the offline unit suite,
the reviewer rule, where a test goes): [`tests/CLAUDE.md`](../../tests/CLAUDE.md).

## Gates

| Gate | Why it exists | Entry point |
|------|---------------|-------------|
| `make check` | the inner-loop gate: lint, strict types on `src/domain` + `src/ports`, the unit and architecture suite without slow tests, offline integration tests, offline G2 | `Makefile` (`check`) |
| `make ci` | `check` plus slow tests and the coverage gates below; the one definition of "green" | `Makefile` (`ci`), `.github/workflows/ci.yml` |
| Global coverage floor | a single floor on the whole tree | `Makefile` (`COVERAGE_FLAGS`) |
| Per-file coverage table | files whose coverage is load-bearing (named by an RFC) cannot silently slip behind the global number | `scripts/check_coverage_thresholds.py::THRESHOLDS` |
| Per-layer floors and never-run check | a global number lets a well-covered layer hide a poorly covered one, and a file no test imports is absent from a plain report; so each layer has its own branch-aware floor and a measured file with no executed line fails | `scripts/check_coverage_layers.py` |
| Architecture tests | hexagonal import rules (REQ-ARCH-*), production wiring and reachability | `tests/unit/` (map in `tests/CLAUDE.md`) |
| Adapter contracts | wire tests at the SDK boundary plus named per-provider rules | `docs/how_to/ADAPTER_WIRE_TESTING.md`, `tests/contracts/adapter_contracts.py` |
| Timing tests | wall-clock and CPU thresholds are flaky under load, so they are off the PR gate | `Makefile` (`test-timing`) |

Floor rule (`scripts/check_coverage_layers.py` docstring): a floor rises; it may be lowered only in the PR that needs it,
with a one-line reason next to the value and the reviewer's OK, so the owner sees the lowering in the diff. When measured coverage is clearly
above a floor the script says so, and the floor is raised in the same change.

## What CI runs

`.github/workflows/ci.yml`:
- **`check` job:** `make ci` on every PR and every push to `main`, then a pass/fail post to Slack. It is the source of the coverage numbers.
- **`timing` job:** `make test-timing` after a merge to `main`, never on a PR (owner decision: it fails on a loaded runner).
- **Docs-only rule.** `scripts/ci/docs_only.py::is_docs_only` is the one definition of "documentation"; the trap is that
  `src/skills/` Markdown is not docs, because the skill loader bundles it into prompts. A docs-only change skips `make ci` (also on a push to `main`, and in the `timing` job) but
  still runs the doc-reading tests, `scripts/ci/docs_only.py::DOC_READING_TESTS`. An empty change list is not docs-only
  (fail safe). A test that starts reading `docs/` must join that tuple (`tests/unit/scripts/test_docs_only.py` fails otherwise); a test reading a root `*.md` or a `CLAUDE.md` is a known gap the scan cannot see.

`.github/workflows/render.yml` (after a merge to `main`, on a PR that touches its inputs, and by hand): ci.yml installs no
node modules or browser, so the DOCX / PDF / HTML render tests skip there. This workflow installs them on a plain runner,
runs exactly those tests and goes red when one skips for a missing prerequisite (`scripts/ci/check_junit_ran.py`). A second
job builds both targets of `Dockerfile.sandbox` locally (never pushed) and runs the `docker`-marked tests; red on a skip.
No secrets, no Slack.

Weekly cloud workflows (`mutation.yml`, `parity.yml`, `spec_drift.yml`) never post to Slack and do not go red on findings:
the result is an artifact and also check-run annotations, which the scheduled routines read (a cloud session cannot download artifacts). `spec_drift.yml` diffs the providers' published specs
against `contracts/spec_drift/` and is read by the provider-contract review.

## Mutation check

Tests are only as good as the faults they catch, so a package is done when every semantic mutant of it is killed or ruled
equivalent. Run: `python -m tests.support.semantic_mutants --module <module> --tests <test files>` (one child pytest per
mutant; exit 1 if any survives). The test files of a module come from `tests/support/mutation_targets.py`, which also
registers the modules the weekly run mutates (`mutation.yml`, one job per module; survivors land in the `survivors`
artifact). Operators: `tests/support/semantic_mutants/operators.py`. A survivor gets a new assertion in the module's
primary test file; only the reviewer rules a mutant equivalent, in the module's `*_equivalent_mutants.txt`. Details and
the registered-target rule: `tests/CLAUDE.md`.

## Living-docs check

Contract: `docs/README.md` § Living docs. Two enforcers run here:
- `.github/workflows/living_docs.yml` runs `scripts/ci/living_docs_pr_check.py` on every PR. A PR that changes a file a
  living doc covers must change that doc or carry `Docs not affected: <doc path> — <reason>` in its description; the
  script prints the paste-ready line for each missing doc.
- `tests/unit/test_living_doc_references.py` (a doc-reading test, so it also runs on docs-only changes) checks headers,
  paths and symbols; the header parser is `scripts/ci/living_docs.py`.

## Friction and fix facts

Two read-only commands turn the first-parent history of `origin/main` into numbers, one unit per first-parent commit (a
merge counts against its first parent, so a whole PR is one unit). They are never a gate; a weekly routine reads them.
- `python -m scripts.quality.friction`: per week, how much existing test code a change to `src` drags along, plus a
  bookkeeping ratio and the most-modified test files. A squash- or rebase-merged PR has no branch commits, so it adds
  nothing to the `test(mechanical)` part.
- `python -m scripts.quality.fix_units`: per `fix` unit that changes `src`, the layers touched, whether a test came with
  it, and the age of the lines it replaced.

Both exit 2 on an unknown ref and on a shallow clone (`git fetch --unshallow`); definitions are in the module docstrings
of `scripts/quality/`.
