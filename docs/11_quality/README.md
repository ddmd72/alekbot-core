# 11 Quality

Quality is enforced by gates that run, not by documents. Where each one lives:

| Gate | What it checks | Where |
|------|----------------|-------|
| `make check` | ruff lint, `mypy --strict` on `src/domain` + `src/ports`, the unit/architecture suite without slow tests, offline G2 | `Makefile` |
| `make ci` | the same plus slow tests and per-layer branch-coverage floors that only rise | `.github/workflows/ci.yml`, `scripts/check_coverage_layers.py` |
| Architecture tests | hexagonal import rules (REQ-ARCH-*), production wiring and reachability | `tests/unit/` (see `tests/CLAUDE.md`) |
| Mutation check | a package is done when every semantic mutant is killed or ruled equivalent | `python -m tests.support.semantic_mutants` |
| Adapter contracts | wire tests at the SDK boundary plus named per-provider contract rules | `docs/how_to/ADAPTER_WIRE_TESTING.md`, `tests/contracts/adapter_contracts.py` |
| Timing tests | wall-clock / CPU thresholds, kept off the everyday gate | `make test-timing` (CI, after a merge to `main`) |
| Friction and fix facts | weekly test friction of `src` changes, and per merged fix: layers, regression test, age of the fixed lines (read-only, from git; never a gate) | `python -m scripts.quality.friction`, `python -m scripts.quality.fix_units` |

Test rules (fakes over call assertions, the offline unit suite, the reviewer rule for
editing an existing test): [`tests/CLAUDE.md`](../../tests/CLAUDE.md). Which commands exist: the root `CLAUDE.md` Commands section.

**CI.** `.github/workflows/ci.yml` runs `make ci` on every push and PR and posts the result to Slack; it is the source of
the coverage numbers. A docs-only change (defined in `scripts/ci/docs_only.py`: everything under `docs/`, root `*.md`,
`.github/**/*.md`, `CLAUDE.md` anywhere except under `src/skills/`) skips `make ci`, also on a push to `main` and in the
`timing` job, but still runs the doc-reading tests listed in `DOC_READING_TESTS`. Three weekly cloud workflows,
`Mutation survivors`, `Firestore parity` and `Spec drift` (the providers' published specs against
`contracts/spec_drift/`), write artifacts that a scheduled routine reads; they never post to Slack.

**Real render.** `.github/workflows/render.yml` (after a merge to `main`, and `workflow_dispatch`) installs node modules and
chrome-headless-shell on a plain runner and runs the DOCX / PDF / HTML render tests that `ci.yml` skips; it is red when one of them
skips for a missing prerequisite. A second job builds both targets of `Dockerfile.sandbox` (loaded locally, never pushed) and runs the `docker`-marked tests against them, red on a skip. No secrets, no Slack.

**Friction and fix facts.** Two read-only commands turn the first-parent history of `origin/main` into numbers; a unit
is one first-parent commit, a merge counted against its first parent (the whole pull request). `friction` prints, per
complete week (Monday to Monday, UTC): `drag` (lines changed in existing test files per line changed in `src`),
`bookkeeping_per_kloc` (existing ledger lines plus `test(mechanical)` branch-commit test lines per 1000 `src` lines),
the radius (existing test files modified per `src`-changing unit), the `def test_` lines added and removed and the share
added in new files, and the most-modified existing test files of the last four weeks; JSON by default, `--format md`
for one line per week. `fix_units` prints, per unit titled `fix:`/`fix(...)`/`fix!` that changes `src`: the layers
touched, whether a test came with it, the age of the lines it deleted or replaced (blamed on its first parent) and the
units that brought those lines in. Both exit 2 on an unknown ref or a shallow history that does not cover the window.
Definitions: the module docstrings in `scripts/quality/`. Who reads it: a weekly routine; its journal is kept outside
this repository.
