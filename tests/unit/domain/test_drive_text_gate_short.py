"""Text-quality gate on short texts, where token variety is not judged (USER_DRIVE_INDEXING_RFC §8.1 step 6,
§5.5 class 3; adversarial review B, M5; round-1 test review B1). Seeded generators (RFC Δ8)."""
import random

import pytest

from src.domain.drive_text_gate import MIN_RESIDUE_LINES, VARIETY_NOT_JUDGED, is_machine_text, measure

SEEDS = range(50)


def _access_log(rng, n):
    return "\n".join(f'10.0.{rng.randint(0, 255)}.{rng.randint(0, 255)} - - [01/Oct/2026:12:{rng.randint(0, 59):02d}:'
                     f'{rng.randint(0, 59):02d} +0000] "{rng.choice(["GET", "POST"])} /static/{rng.choice("abc")}.js '
                     f'HTTP/1.1" {rng.choice([200, 304])} {rng.randint(100, 9000)}' for _ in range(n))


@pytest.mark.parametrize("seed", SEEDS)
def test_short_access_log_is_machine_text(seed):
    rng = random.Random(seed)
    text = _access_log(rng, rng.randint(MIN_RESIDUE_LINES + 1, 35))   # ~11 tokens a line: under the token floor
    assert measure(text).variety == VARIETY_NOT_JUDGED
    assert is_machine_text(text)


@pytest.mark.parametrize("seed", SEEDS)
def test_short_app_log_with_levels_is_machine_text(seed):
    rng = random.Random(seed)
    lines = [f"[2026-10-01T09:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z] {rng.choice(['INFO', 'ERROR'])} "
             f"scheduler: job={rng.randint(1, 9999)} queue=drive-sync attempt={rng.randint(1, 5)} "
             f"result={rng.choice(['done', 'retry'])}" for _ in range(rng.randint(25, 60))]
    text = "\n".join(lines)
    assert measure(text).variety == VARIETY_NOT_JUDGED
    assert is_machine_text(text)


@pytest.mark.parametrize("seed", SEEDS)
def test_short_receipt_passes(seed):
    rng = random.Random(seed)
    items = ["Leche entera 1L", "Pan de molde", "Huevos L docena", "Tomate rama", "Aceite de oliva 1L", "Queso curado"]
    lines = ["MERCADONA S.A.", "C/ Colón 12, Valencia"]
    lines += [f"{rng.randint(1, 3)} {rng.choice(items)}          {rng.uniform(0.5, 9.0):.2f}" for _ in range(12)]
    lines += ["TOTAL (€)   34,17", "TARJETA BANCARIA   34,17", "Gracias por su visita"]
    assert not is_machine_text("\n".join(lines))


@pytest.mark.parametrize("seed", SEEDS)
def test_short_same_template_ledger_passes(seed):
    """Above MIN_RESIDUE_LINES, under the token floor: every template line carries an amount, so it is a ledger."""
    rng = random.Random(seed)
    lines = ["Payroll October 2026"]
    lines += [f"Monthly salary payment employee {rng.randint(1, 40)}  {rng.randint(1000, 4000)}.00 EUR"
              for _ in range(rng.randint(MIN_RESIDUE_LINES + 1, 40))]
    text = "\n".join(lines)
    assert measure(text).variety == VARIETY_NOT_JUDGED
    assert not is_machine_text(text)


def test_short_text_with_few_plain_lines_is_not_judged_on_them():
    """A short ledger with a handful of repeated plain lines: too few plain lines to judge their repetition."""
    lines = [f"Item {i} delivered and invoiced  {10 + i}.50 EUR" for i in range(25)]
    lines += ["Please pay within thirty days of receipt"] * (MIN_RESIDUE_LINES - 1)
    assert not is_machine_text("\n".join(lines))
