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

Test rules (fakes over call assertions, the offline unit suite, the reviewer rule for
editing an existing test): [`tests/CLAUDE.md`](../../tests/CLAUDE.md). Which commands exist: the root `CLAUDE.md` Commands section.

**CI.** `.github/workflows/ci.yml` runs `make ci` on every push and PR and posts the result to Slack; it is the source of
the coverage numbers. A docs-only change (defined in `scripts/ci/docs_only.py`: everything under `docs/`, root `*.md`,
`.github/**/*.md`, `CLAUDE.md` anywhere except under `src/skills/`) skips `make ci`, also on a push to `main` and in the
`timing` job, but still runs the doc-reading tests listed in `DOC_READING_TESTS`. Two weekly cloud workflows,
`Mutation survivors` and `Firestore parity`, write artifacts that a scheduled routine reads; they never post to Slack.
