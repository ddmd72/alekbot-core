# 11 Quality

Quality is enforced by gates that run, not by documents. Where each one lives:

| Gate | What it checks | Where |
|------|----------------|-------|
| `make check` | ruff lint, `mypy --strict` on `src/domain` + `src/ports`, the unit/architecture suite without slow tests, offline G2 | `Makefile`; mandatory before a commit |
| `make ci` | the same plus slow tests and per-layer branch-coverage floors that only rise | `.github/workflows/ci.yml`, `scripts/check_coverage_layers.py` |
| Architecture tests | hexagonal import rules (REQ-ARCH-*), production wiring and reachability | `tests/unit/` (see `tests/CLAUDE.md`) |
| Mutation check | a package is done when every semantic mutant is killed or ruled equivalent | `python -m tests.support.semantic_mutants` |
| Adapter contracts | wire tests at the SDK boundary plus named per-provider contract rules | `docs/how_to/ADAPTER_WIRE_TESTING.md`, `tests/contracts/adapter_contracts.py` |
| Timing tests | wall-clock / CPU thresholds, kept off the everyday gate | `make test-timing` (CI, after a merge to `main`) |

Test rules (fakes over call assertions, the offline unit suite, the reviewer rule for
editing an existing test): [`tests/CLAUDE.md`](../../tests/CLAUDE.md). Which commands exist: the root `CLAUDE.md` Commands section.
