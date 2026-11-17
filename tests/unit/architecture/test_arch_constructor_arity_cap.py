"""A constructor in `src/handlers/`, `src/services/` or `src/composition/` takes at most 12 parameters, and so does a
`create_*` method of a class in `src/composition/` (composition-root assessment D1: over-injection is a class doing too
much; the 49-parameter `UserAgentFactory` and the 34-parameter `WorkerHandler` grew one keyword at a time).

The count excludes `self`/`cls`; a positional, a keyword-only, `*args` and `**kwargs` each count 1. A class with no
`__init__` of its own (a dataclass) is not checked.

Allowlist `constructor_arity_allowlist.txt`, one line per offender: `<path>::<Class>[.<method>]  # <count>  # <reason>`
(`<path>::<Class>` is its `__init__`). The count is exact: a constructor that grew fails, and one that shrank fails until
its line is lowered. A line whose offence is gone (12 or fewer) fails: delete it.
"""
import ast
from pathlib import Path
from typing import Dict

import pytest

from tests.support import arch_ratchet

ALLOWLIST = Path(__file__).with_name("constructor_arity_allowlist.txt")
CAP = 12
LAYERS = ("src/handlers", "src/services", "src/composition")
FIX = (f"Do not add a parameter past {CAP}: group the dependencies that serve one task into a smaller class or a "
       "frozen bundle built in composition/, or move the logic that needs them out of this class.")


def arity(fn: ast.AST) -> int:
    """Parameters of `fn`, without the `self`/`cls` of a method that is not a staticmethod."""
    args = fn.args
    n = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs) + (args.vararg is not None) + (args.kwarg is not None)
    static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in fn.decorator_list)
    return n - (1 if (args.posonlyargs or args.args) and not static else 0)


def offenders(module: ast.AST, path: str, cap: int = CAP) -> Dict[str, int]:
    found = {}
    composition = path.startswith("src/composition/")
    for qualname, fn, owner in arch_ratchet.functions(module):
        if owner is None:
            continue
        if fn.name == "__init__":
            key = f"{path}::{qualname[: -len('.__init__')]}"
        elif composition and fn.name.startswith("create_"):
            key = f"{path}::{qualname}"
        else:
            continue
        n = arity(fn)
        if n > cap:
            found[key] = n
    return found


def scan() -> Dict[str, int]:
    found: Dict[str, int] = {}
    for path in arch_ratchet.sources(*LAYERS):
        found.update(offenders(arch_ratchet.tree(path), path))
    return found


@pytest.mark.slow  # parses a whole layer of src/: over 0.5 s with coverage on
def test_no_constructor_grows_past_the_cap():
    listed = arch_ratchet.load(ALLOWLIST, counted=True)

    problems = arch_ratchet.compare(scan(), listed, allowlist=ALLOWLIST.name, unit="parameters", fix=FIX)

    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------- the check, on inline sources

def _found(source: str, path: str = "src/services/x.py") -> Dict[str, int]:
    return offenders(ast.parse(source), path)


def test_every_parameter_kind_counts_once_and_self_does_not():
    params = ", ".join(f"p{i}" for i in range(10))
    source = f"class A:\n    def __init__(self, {params}, *args, k, **kwargs):\n        pass\n"

    assert _found(source) == {"src/services/x.py::A": 13}


def test_twelve_parameters_pass():
    params = ", ".join(f"p{i}" for i in range(12))

    assert _found(f"class A:\n    def __init__(self, {params}):\n        pass\n") == {}


def test_a_create_method_counts_in_composition_only_and_a_staticmethod_keeps_its_first_parameter():
    params = ", ".join(f"p{i}" for i in range(13))
    source = f"class F:\n    @staticmethod\n    def create_adapter({params}):\n        pass\n"

    assert _found(source, "src/composition/f.py") == {"src/composition/f.py::F.create_adapter": 13}
    assert _found(source, "src/services/f.py") == {}


def test_a_grown_a_shrunk_a_gone_and_a_new_constructor_are_reported():
    listed = arch_ratchet.parse("a::A  # 20  # r1 r2 r3\nb::B  # 20  # r1 r2 r3\nc::C  # 20  # r1 r2 r3\n", counted=True)

    problems = arch_ratchet.compare({"a::A": 21, "b::B": 19, "d::D": 13}, listed, allowlist="L", unit="parameters", fix="F")

    assert problems == [
        "a::A: 21 parameters, but its line in L allows 20; the list only shrinks. F",
        "b::B: 19 parameters, its line in L says 20: lower the line to 19",
        "d::D: 13 parameters, and it is not in L. F",
        "c::C is listed in L (line 3) but its offence is gone: delete the line",
    ]
