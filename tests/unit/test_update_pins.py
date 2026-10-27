"""`UPDATE_PINS=1` rewrites the boot test's url_map pin; without it a drifted pin is only reported."""
import pytest

from tests.unit import test_main_boots_offline as boot_test
from tests.unit.test_main_boots_offline import Boot, _check_url_map, _update_pins_if_asked

HEADER = "# invented header line"
BOOTED = ["GET /alpha", "GET /health", "GET /health", "POST /zeta"]
DRIFTED = "\n".join([HEADER, "GET /alpha", "GET /gone"]) + "\n"


@pytest.fixture
def pin(tmp_path, monkeypatch):
    path = tmp_path / "url_map.txt"
    path.write_text(DRIFTED)
    monkeypatch.setattr(boot_test, "URL_MAP", path)
    return path


def test_with_the_flag_a_drifted_pin_is_rewritten_and_then_passes(pin, monkeypatch):
    monkeypatch.setenv("UPDATE_PINS", "1")
    boot = Boot(rules=list(BOOTED))
    _update_pins_if_asked(boot)
    assert _check_url_map(boot) == []
    assert pin.read_text().splitlines() == [HEADER] + sorted(BOOTED)


def test_without_the_flag_a_drifted_pin_is_untouched_and_reported(pin, monkeypatch):
    monkeypatch.delenv("UPDATE_PINS", raising=False)
    boot = Boot(rules=list(BOOTED))
    _update_pins_if_asked(boot)
    problems = _check_url_map(boot)
    assert pin.read_text() == DRIFTED
    assert any("not served: GET /gone" in p for p in problems)
    assert any("not pinned: POST /zeta" in p for p in problems)
