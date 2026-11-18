"""H2 on the Firestore test database: every store kit of `tests/support/store_inventory.py` `KITS`, run from its own file
against the real adapter (TEST_COVERAGE_SYSTEMIC_RFC §4 "What runs there", P12). This file holds no test: the hook in
`tests/cloud/conftest.py` collects it as a `KitSuite`, one child per kit file, so the kits stay single-source.

A kit runs here once it builds its store over the `kit_db` fixture (`tests/support/kit_backend.py`); a kit that does not
yet is reported as one skipped collector. Its `h1_only` tests are skipped, and a test that still builds an `EvalDb` fails.
Run with `make test-cloud` (serial, one runner at a time).
"""
