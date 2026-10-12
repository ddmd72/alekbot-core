"""
Drive search eval (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §17.8, §24.11; RFC Δ13).

Runs the real tick (DriveIndexingService, with the real parser, reader and triage from Slice 2+4) and the real search
(DriveSearchService) on the in-memory repository over a synthetic, PII-free drive (tests/fixtures/drive_eval/tree.json
and its files/), then measures recall@K for the G1 cases (find a file) and the G2 cases (answer from content: the
expected substring is in the card's window). A regression guard, not a decision tool: the first run records the bars,
later runs must not fall below them. G3 (does Smart search the drive when it should) is smart_dry_run.py.

    venv/bin/python -m scripts.validation.drive_eval            # compare against the recorded bars
    venv/bin/python -m scripts.validation.drive_eval --record   # (re)record the bars
"""
