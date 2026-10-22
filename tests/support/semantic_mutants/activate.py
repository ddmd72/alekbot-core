"""pytest plugin a mutant child process loads (`-p tests.support.semantic_mutants.activate`).

Inert unless SEMANTIC_MUTANT_MODULE and SEMANTIC_MUTANT_NUMBER are set. It installs the import hook while the plugin is
imported, which is before any conftest or test module, and writes a small JSON report at the end of the session
(SEMANTIC_MUTANT_REPORT): whether the mutated module was actually compiled and loaded. A mutant the tests never load
cannot be killed, so the driver reports it as an error and not as a survivor.
"""

import json
import os

from . import loader

_MODULE = os.environ.get("SEMANTIC_MUTANT_MODULE", "")
_NUMBER = os.environ.get("SEMANTIC_MUTANT_NUMBER", "")
_FINDER = loader.install(_MODULE, int(_NUMBER)) if _MODULE and _NUMBER else None


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("SEMANTIC_MUTANT_REPORT")
    if _FINDER is not None and path:
        with open(path, "w") as out:
            json.dump({"module": _MODULE, "number": int(_NUMBER), "loaded": _FINDER.loaded}, out)
