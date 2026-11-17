"""The main service's deploy literals (its URL key, regions, queue-suffix rule) live in `deploy_topology.py` only."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OWNER = ROOT / "src" / "composition" / "deploy_topology.py"
# Out-of-scope entry points that repeat a literal on their own (their own processes); file -> reason.
ALLOWLIST: dict[str, str] = {}

LITERALS = {
    "service URL key": re.compile(r"CLOUD_RUN_SERVICE_URL"),
    "region": re.compile(r"us-central1"),
    "slack intake region": re.compile(r"europe-west1"),
    "queue suffix rule": re.compile(r"is_development\s+else\s+[\"']prod[\"']"),
}


def _scanned() -> list[Path]:
    return sorted(p for p in [*(ROOT / "src" / "composition").glob("*.py"), ROOT / "main.py"] if p != OWNER)


def _hits(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [name for name, rx in LITERALS.items() if rx.search(text)]


def test_no_composition_module_or_main_repeats_a_deploy_literal():
    offenders = {str(p.relative_to(ROOT)): hits for p in _scanned()
                 if (hits := _hits(p)) and str(p.relative_to(ROOT)) not in ALLOWLIST}
    assert not offenders, f"read these from DeployTopology instead: {offenders}"


def test_every_allowlisted_file_still_repeats_a_literal():
    stale = [name for name in ALLOWLIST if not _hits(ROOT / name)]
    assert not stale, f"drop from ALLOWLIST: {stale}"


def test_the_owner_still_holds_every_literal():
    assert _hits(OWNER) == list(LITERALS)
