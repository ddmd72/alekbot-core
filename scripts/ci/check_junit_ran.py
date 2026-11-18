"""Fails a render-job step when the tests it was meant to run did not run (.github/workflows/render.yml).

Usage: python scripts/ci/check_junit_ran.py <junit.xml> <test file>... [--allow-skip REGEX]
Every listed test file needs at least one passed test; a skipped test is an error unless its reason matches the --allow-skip
regex. A missing or unreadable report fails: a missing install must never read as green.
"""
import re
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional


def check(junit_path: str, files: List[str], allow_skip: Optional[str]) -> List[str]:
    try:
        cases = ET.parse(junit_path).getroot().iter("testcase")
    except (OSError, ET.ParseError) as exc:
        return [f"no readable junit report {junit_path}: {exc}"]
    allowed = re.compile(allow_skip) if allow_skip else None
    passed: Dict[str, int] = {f: 0 for f in files}
    problems: List[str] = []
    for case in cases:
        module = case.get("classname", "")
        owner = next((f for f in files if module == f[:-3].replace("/", ".") or module.startswith(f[:-3].replace("/", ".") + ".")), None)
        skipped = case.find("skipped")
        if skipped is not None:
            reason = skipped.get("message", "")
            if not (allowed and allowed.search(reason)):
                problems.append(f"skipped: {module}::{case.get('name')}: {reason}")
        elif owner and case.find("failure") is None and case.find("error") is None:
            passed[owner] += 1
    problems += [f"no test passed in {f}" for f, n in passed.items() if n == 0]
    return problems


def main(argv: List[str]) -> int:
    allow = None
    if "--allow-skip" in argv:
        at = argv.index("--allow-skip")
        allow = argv[at + 1]
        argv = argv[:at] + argv[at + 2:]
    problems = check(argv[0], argv[1:], allow)
    for problem in problems:
        print(f"::error title=render job::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
