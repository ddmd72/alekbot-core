"""Parser@2 costs no more than v1 on a large page (review M4): the page is parsed once and the chosen element
converted in place. v1 (markitdown's HtmlConverter on the whole page) and parser@2 run on the same synthetic page,
each in a fresh process, measuring CPU time and the resident memory the conversion adds over the imports; parser@2
stays within v1 + 10 % on both and under the child's limits.

CPU is compared as the minimum of several conversions per mode, never one sample: noise on a shared machine (other
test workers, a busy sibling core) only adds time, so the minimum is the cost of the code (Python's `timeit` takes the
minimum for the same reason). The children run in the order v1, v2, v2, v1, each converting the page `_CONVERSIONS`
times, so a drift in the machine's load reaches both modes alike. Memory is compared per pair of children, as the
first conversion of a fresh process measures it."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.adapters.subprocess_content_extractor import EXTRACT_CPU_LIMIT_S, EXTRACT_MEMORY_LIMIT_BYTES

_ROOT = Path(__file__).resolve().parents[3]
_PAGE_BYTES = 250_000
_CONVERSIONS = 2                       # per child: the first measures the memory, every one the CPU
_ORDER = ("v1", "v2", "v2", "v1")      # interleaved, so neither mode runs only while the machine is busier
_MEASURE = r"""
import gc, json, resource, sys, time
mode, shape, size, conversions = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
if shape == "divs":
    unit = "<div class=a><span>word</span> text <a href='https://example.org/x'>link</a></div>\n"
    page = "<html><body>" + unit * (size // len(unit)) + "</body></html>"
else:
    row = "<tr><td>a</td><td>12.5</td><td>x</td></tr>"
    page = "<html><body><main><table>" + row * (size // len(row)) + "</table></main></body></html>"
if mode == "v1":
    from markitdown.converters import HtmlConverter
    convert = lambda: HtmlConverter().convert_string(page).markdown
else:
    import bs4, markdownify
    from src.adapters.drive_parser.html_text import html_page_text
    convert = lambda: html_page_text(page)[0]
scale = 1024 if sys.platform.startswith("linux") else 1         # ru_maxrss: KiB on Linux, bytes on macOS
cpu, added, chars = [], None, 0
for _ in range(conversions):
    gc.collect()                                                # each conversion starts from the same heap
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    started = time.process_time()
    out = convert()
    cpu.append(time.process_time() - started)
    if added is None:                                           # a high-water mark: only a fresh process's first
        added = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale - before
    chars = len(out)
    del out
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
print(json.dumps({"cpu": cpu, "added": added, "peak": peak, "chars": chars}))
"""


def _measure(mode: str, shape: str) -> dict:
    proc = subprocess.run([sys.executable, "-c", _MEASURE, mode, shape, str(_PAGE_BYTES), str(_CONVERSIONS)],
                          cwd=_ROOT, capture_output=True, timeout=300, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("shape", ["divs", "table"])
def test_parser_v2_costs_no_more_than_v1(shape):
    runs: dict = {"v1": [], "v2": []}
    for mode in _ORDER:
        runs[mode].append(_measure(mode, shape))
    v1_cpu = min(cpu for run in runs["v1"] for cpu in run["cpu"])
    v2_cpu = min(cpu for run in runs["v2"] for cpu in run["cpu"])
    assert v2_cpu <= 1.10 * v1_cpu, (v1_cpu, v2_cpu, runs)
    for v1, v2 in zip(runs["v1"], runs["v2"]):
        assert v2["chars"] > 0
        assert v2["added"] <= 1.10 * v1["added"] + 8 * 1024 * 1024, (v1, v2)   # RSS is page-granular: 8 MiB slack
        assert max(v2["cpu"]) < EXTRACT_CPU_LIMIT_S and v2["peak"] < EXTRACT_MEMORY_LIMIT_BYTES
