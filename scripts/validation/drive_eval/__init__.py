"""
Drive search eval (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §17.8; plan Δ13).

Runs the real tick (DriveIndexingService) and the real search (DriveSearchService) on the in-memory
repository over a synthetic, PII-free drive (tests/fixtures/drive_eval/tree.json), then measures
recall@K for the G1 gold cases. A regression guard, not a decision tool: the first run records the
bars, later runs must not fall below them.

    venv/bin/python -m scripts.validation.drive_eval            # compare against the recorded bars
    venv/bin/python -m scripts.validation.drive_eval --record   # (re)record the bars
"""
