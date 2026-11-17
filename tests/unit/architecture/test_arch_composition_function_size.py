"""No function in `src/composition/`, nor `main()` in `main.py`, is longer than 150 lines (composition-root assessment D6:
a builder that keeps growing by "put it next to its first dependency" ends as the 946-line `build_application_graph`; a
new subsystem gets its own `*_setup.py` instead).

Length is `end_lineno - lineno + 1` of the `def` (decorators excluded); a nested function is measured on its own too.

Allowlist `composition_function_size_allowlist.txt`: `<path>::<qualified function name>  # <length>  # <reason>`. The
listed length is a ceiling: only growth past it fails. A function that shrinks but stays over 150 lines does not fail
(lowering its line is welcome, not required). A line whose offence is gone (150 lines or fewer, or the function is
gone) fails: delete it.
"""
import ast
from pathlib import Path
from typing import Dict

from tests.support import arch_ratchet

ALLOWLIST = Path(__file__).with_name("composition_function_size_allowlist.txt")
LIMIT = 150
FIX = (f"Do not grow it: move a section into its own *_setup.py builder that returns one frozen bundle, so no "
       f"function in the composition root is over {LIMIT} lines.")


def long_functions(module: ast.AST, path: str, *, only: str = "", limit: int = LIMIT) -> Dict[str, int]:
    found = {}
    for qualname, fn, _ in arch_ratchet.functions(module):
        length = fn.end_lineno - fn.lineno + 1
        if length > limit and (not only or qualname == only):
            found[f"{path}::{qualname}"] = length
    return found


def scan() -> Dict[str, int]:
    found = long_functions(arch_ratchet.tree("main.py"), "main.py", only="main")
    for path in arch_ratchet.sources("src/composition"):
        found.update(long_functions(arch_ratchet.tree(path), path))
    return found


def test_no_composition_function_grows_past_its_ceiling():
    listed = arch_ratchet.load(ALLOWLIST, counted=True)

    problems = arch_ratchet.compare(scan(), listed, allowlist=ALLOWLIST.name, unit="lines", fix=FIX, ceiling=True)

    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------- the check, on inline sources

def test_length_counts_from_the_def_to_the_last_line_and_nested_functions_count_alone():
    body = "\n".join(f"        x{i} = {i}" for i in range(4))
    source = f"@decorator\ndef outer():\n    def inner():\n{body}\n    return inner\n"

    assert long_functions(ast.parse(source), "p.py", limit=4) == {"p.py::outer": 7, "p.py::outer.inner": 5}


def test_a_shrink_above_the_limit_passes_and_growth_or_a_shrink_below_it_fails():
    listed = arch_ratchet.parse("a::f  # 300  # r1 r2 r3\nb::g  # 300  # r1 r2 r3\n", counted=True)

    assert arch_ratchet.compare({"a::f": 200, "b::g": 300}, listed, allowlist="L", unit="lines", fix="F", ceiling=True) == []
    assert arch_ratchet.compare({"a::f": 301}, listed, allowlist="L", unit="lines", fix="F", ceiling=True) == [
        "a::f: 301 lines, but its line in L allows 300; the list only shrinks. F",
        "b::g is listed in L (line 2) but its offence is gone: delete the line",
    ]
