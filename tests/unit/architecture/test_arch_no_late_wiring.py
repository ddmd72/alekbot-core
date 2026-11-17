"""Nothing is wired after construction (composition-root assessment D3: a setter called later hides the order things must
be built in, and a dependency that arrives late is `None` until then).

(a) In `src/composition/` and `main.py`, a call of a method named `attach_*`, `set_*`, `register_*` or `register` is
allowed only on `late_wiring_calls_allowlist.txt`: `<path>::<callee>  # <count>  # <reason>`, where `<callee>` is the
called expression as written (`worker_handler.attach_slack_worker`) and the count is exact. The permanent exceptions
(the Registry pattern, the one real router-factory cycle and the late Telegram platform binding of `register_late_platform`,
owner ruling 2026-11-17) carry the count `*` and a reason starting with `permanent`.

(b) In `src/services/` and `src/handlers/`, a method named `attach_*`, `set_*` or `register_*` is allowed only on
`late_wiring_setters_allowlist.txt`: `<path>::<Class>.<method>  # <reason>`.

A line whose offence is gone fails: delete it.
"""
import ast
import re
from pathlib import Path
from typing import Dict

from tests.support import arch_ratchet

CALLS = Path(__file__).with_name("late_wiring_calls_allowlist.txt")
SETTERS = Path(__file__).with_name("late_wiring_setters_allowlist.txt")
# A module whose text lacks these cannot hold an offence and is not parsed (keeps the guard fast).
CALL_TEXT = re.compile(r"\.(?:attach_|set_|register)")
SETTER_TEXT = re.compile(r"\bdef\s+(?:attach_|set_|register_)")
FIX_CALL = ("Do not add a new attach/set/register call: pass the dependency in the constructor and build the object after "
            "its dependencies.")
FIX_SETTER = ("Do not add a new setter: take the dependency as a constructor parameter (composition/ builds the "
              "dependency first).")


def wiring_call(name: str) -> bool:
    return name == "register" or name.startswith(("attach_", "set_", "register_"))


def calls(module: ast.AST, path: str) -> Dict[str, int]:
    found: Dict[str, int] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and wiring_call(node.func.attr):
            key = f"{path}::{ast.unparse(node.func)}"
            found[key] = found.get(key, 0) + 1
    return found


def setters(module: ast.AST, path: str) -> Dict[str, int]:
    return {f"{path}::{qualname}": 1 for qualname, fn, owner in arch_ratchet.functions(module)
            if owner is not None and fn.name.startswith(("attach_", "set_", "register_"))}


def test_composition_and_main_call_only_the_listed_late_wiring():
    found: Dict[str, int] = {}
    for path in arch_ratchet.sources("src/composition", "main.py"):
        if CALL_TEXT.search((arch_ratchet.ROOT / path).read_text()):
            found.update(calls(arch_ratchet.tree(path), path))

    problems = arch_ratchet.compare(found, arch_ratchet.load(CALLS, counted=True), allowlist=CALLS.name, unit="calls",
                                    fix=FIX_CALL)

    assert not problems, "\n".join(problems)


def test_services_and_handlers_define_only_the_listed_setters():
    found: Dict[str, int] = {}
    for path in arch_ratchet.sources("src/services", "src/handlers"):
        if SETTER_TEXT.search((arch_ratchet.ROOT / path).read_text()):
            found.update(setters(arch_ratchet.tree(path), path))

    problems = arch_ratchet.compare(found, arch_ratchet.load(SETTERS, counted=False), allowlist=SETTERS.name,
                                    unit="definition", fix=FIX_SETTER)

    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------- the check, on inline sources

def test_the_call_scan_keys_by_the_callee_as_written_and_counts():
    source = ("def build():\n    wh.attach_slack_worker(a)\n    wh.attach_slack_worker(b)\n    reg.register('x', y)\n"
              "    app.container.svc.set_repo(r)\n    d.setdefault('k', 1)\n    wh.registered(1)\n    attach_x(1)\n")

    assert calls(ast.parse(source), "p.py") == {
        "p.py::wh.attach_slack_worker": 2, "p.py::reg.register": 1, "p.py::app.container.svc.set_repo": 1}


def test_the_text_filters_keep_every_module_the_scans_would_report():
    assert CALL_TEXT.search("wh.attach_x(1)") and CALL_TEXT.search("r.register(1)") and CALL_TEXT.search("s.set_y(2)")
    assert SETTER_TEXT.search("    async def set_x(self, x):") and SETTER_TEXT.search("def attach_y(self, y):")
    assert SETTER_TEXT.search("    async def register_x(self, x):")
    assert not SETTER_TEXT.search("def settle(self):")


def test_the_setter_scan_sees_methods_only():
    source = "def set_x(a):\n    pass\nclass S:\n    def attach_y(self, y):\n        self.y = y\n    def settle(self):\n        pass\n"

    assert setters(ast.parse(source), "p.py") == {"p.py::S.attach_y": 1}


def test_the_setter_scan_sees_register_methods_sync_and_async():
    source = ("def register_x(a):\n    pass\nclass S:\n    def register_platform(self, p):\n        pass\n"
              "    async def register_new_user(self):\n        pass\n    def registered(self):\n        pass\n")

    assert setters(ast.parse(source), "p.py") == {"p.py::S.register_platform": 1, "p.py::S.register_new_user": 1}


def test_an_unlisted_register_method_fails_a_listed_one_passes_and_a_stale_line_fails():
    found = setters(ast.parse("class S:\n    def register_a(self):\n        pass\n    def register_b(self):\n"
                              "        pass\n"), "p.py")
    listed = arch_ratchet.parse("p.py::S.register_a  # registry pattern, not a dependency setter\n"
                                "p.py::S.register_gone  # registry pattern, not a dependency setter\n", counted=False)

    problems = arch_ratchet.compare(found, listed, allowlist="L", unit="definition", fix="F")

    assert any("S.register_b" in line for line in problems)
    assert any("S.register_gone" in line for line in problems)
    assert not any("S.register_a" in line for line in problems)


def test_a_permanent_line_takes_any_count_and_only_a_permanent_line_may():
    listed = arch_ratchet.parse("p.py::reg.register  # *  # permanent: Registry pattern\n", counted=True)

    assert arch_ratchet.compare({"p.py::reg.register": 7}, listed, allowlist="L", unit="calls", fix="F") == []
    try:
        arch_ratchet.parse("p.py::wh.attach_x  # *  # composition-root refactor (assessment D3)\n", counted=True)
    except ValueError as error:
        assert "permanent" in str(error)
    else:
        raise AssertionError("a `*` count without a permanent reason was accepted")
