"""The import hook that loads ONE mutant of ONE module.

`install(module, number)` puts a `MetaPathFinder` first on `sys.meta_path`. It answers for exactly `module` (every other
name falls through to the normal finders), asks the regular path finder for the module's spec, and swaps in a source
loader that compiles the mutated AST instead of the file's source. Bytecode caches are neither read nor written, so a
mutant never reaches `__pycache__`, and the original is never served from a stale `.pyc`.

The module must not be imported yet when the hook goes in (`install` raises otherwise): a module already in
`sys.modules` would silently run unmutated.
"""

import importlib.abc
import importlib.machinery
import sys
from typing import Optional

from .operators import apply_mutant


class _MutantLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname: str, path: str, number: int, on_load):
        super().__init__(fullname, path)
        self._number = number
        self._on_load = on_load

    def get_code(self, fullname):  # no .pyc in or out
        return self.source_to_code(self.get_data(self.path), self.path)

    def source_to_code(self, data, path, *, _optimize=-1):
        source = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
        tree = apply_mutant(source, self._number)
        self._on_load()
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


class MutantFinder(importlib.abc.MetaPathFinder):
    def __init__(self, module: str, number: int):
        self.module = module
        self.number = number
        self.loaded = False

    def _mark_loaded(self) -> None:
        self.loaded = True

    def find_spec(self, fullname, path=None, target=None) -> Optional[importlib.machinery.ModuleSpec]:
        if fullname != self.module:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return None  # not a plain source module: leave it alone (the driver reports "never loaded")
        spec.loader = _MutantLoader(fullname, spec.loader.path, self.number, self._mark_loaded)
        return spec


def install(module: str, number: int) -> MutantFinder:
    if module in sys.modules:
        raise RuntimeError(f"{module} is already imported: the mutant cannot be loaded in this process")
    finder = MutantFinder(module, number)
    sys.meta_path.insert(0, finder)
    return finder


def uninstall(finder: MutantFinder) -> None:
    if finder in sys.meta_path:
        sys.meta_path.remove(finder)
