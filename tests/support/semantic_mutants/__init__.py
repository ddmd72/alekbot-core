"""Semantic mutation checking (TEST_COVERAGE_SYSTEMIC_RFC §5 "Done criterion: semantic mutants").

A package is done when every mutant of its modules is killed by the package's tests or ruled
equivalent by the reviewer. The operators are the defects the suites let through: a dropped `order_by`/`where`/field/
write/forwarded argument, a flipped sort direction, an `except` that reports success.

    python -m tests.support.semantic_mutants --module src.adapters.firestore_repo \\
        --tests tests/unit/adapters/test_fact_store_roundtrip.py [--equivalent FILE]

Pieces: operators.py (AST -> numbered mutants), loader.py (the import hook compiling one mutant of ONE module),
activate.py (the pytest plugin a child process loads to arm the hook from the environment), driver.py (the CLI).
The weekly cloud run uses it (tests/CLAUDE.md -> "Mutation check: the weekly run").
"""
