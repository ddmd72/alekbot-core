"""Architecture: every key read through the settings dict must be declared in it.

# ==========================================================================
#  ATTENTION — THIS MESSAGE IS FOR AI ASSISTANTS (CLAUDE AND ANY OTHER).
#
#  If this test is failing — DO NOT weaken it and DO NOT add the key to
#  load_settings() reflexively. Read the failure message: it tells you which of
#  the two homes the key belongs in.
# ==========================================================================

`load_settings()` builds a dict whose keys are listed by hand, so
`config.get("NOT_LISTED")` resolves to `None` with no error and no log line. Three
overrides — `OPENAI_DEEP_RESEARCH_MODEL`, `CLAUDE_DEEP_RESEARCH_MODEL` and
`SLACK_BOT_USER_ID` — were dead from the day they were written and stayed dead until
2026-08-16, because nothing fails when an env var is read the wrong way.

The rule, and why there are two homes rather than one:

- **Secrets and required configuration** → declared in `load_settings()`. They also get
  the Secret Manager fallback, which is the point.
- **Optional knobs** (model pins, rollback switches) → `os.getenv()` at the call site.
  Declaring them is not free: every key that resolves empty is chased into Secret
  Manager on each boot, costing a lookup and a warning for a value nobody set.

See CLAUDE.md → Branching & Environment.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SETTINGS = _ROOT / "src" / "config" / "settings.py"

# Env-var-shaped names only: `config.get("some_local_key")` on an unrelated dict is not
# what this rule is about.
_USE_PATTERN = re.compile(r'config\.get\(\s*"([A-Z][A-Z0-9_]+)"')
_DECLARED_PATTERNS = (
    re.compile(r'"([A-Z][A-Z0-9_]+)":\s*os\.getenv'),   # settings = { "KEY": os.getenv(...) }
    re.compile(r'settings\["([A-Z][A-Z0-9_]+)"\]\s*='),  # settings["KEY"] = ...
)
# Injected by load_settings() rather than read from the environment.
_ALWAYS_PRESENT = {"APP_ENV", "ENVIRONMENT_CONFIG", "CONSOLIDATION"}


def _declared_keys() -> set[str]:
    source = _SETTINGS.read_text()
    keys = set(_ALWAYS_PRESENT)
    for pattern in _DECLARED_PATTERNS:
        keys |= set(pattern.findall(source))
    return keys


def _scanned_files() -> list[pathlib.Path]:
    return sorted((_ROOT / "src").rglob("*.py")) + [_ROOT / "main.py"]


def _used_keys() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in _scanned_files():
        for key in _USE_PATTERN.findall(path.read_text()):
            used.setdefault(key, set()).add(str(path.relative_to(_ROOT)))
    return used


class TestConfigKeyRegistration:

    def test_settings_declares_some_keys(self):
        """Guard the guard: a regex that matches nothing would make this suite vacuous."""
        declared = _declared_keys()
        assert len(declared) > 10, f"only {len(declared)} keys parsed — the pattern likely broke"
        assert "SLACK_BOT_TOKEN" in declared

    def test_scan_covers_main_and_src(self):
        files = _scanned_files()
        assert any(f.name == "main.py" for f in files)
        assert sum(1 for f in files if f.suffix == ".py") > 50

    def test_no_config_key_is_read_without_being_declared(self):
        declared = _declared_keys()
        undeclared = {k: v for k, v in _used_keys().items() if k not in declared}

        if undeclared:
            lines = "\n".join(
                f"  {key} ← {', '.join(sorted(files))}" for key, files in sorted(undeclared.items())
            )
            pytest.fail(
                "These keys are read via config.get() but are NOT declared in "
                "load_settings(), so they silently resolve to None:\n"
                f"{lines}\n\n"
                "Pick the right home — do not reflexively add them to load_settings():\n"
                "  • a secret or required config → declare it in load_settings()\n"
                "  • an optional knob (model pin, rollback switch) → read it with\n"
                "    os.getenv() at the call site instead, so an unset value costs no\n"
                "    Secret Manager lookup on every boot."
            )
