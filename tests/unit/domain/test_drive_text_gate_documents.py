"""Text-quality gate: financial documents pass, machine dumps still fail, and the gate is linear in the text
(USER_DRIVE_INDEXING_RFC §8.1 step 6, §5.5 class 3; adversarial review B, M1 and M5). Seeded generators (RFC Δ8)."""
import random
import string
import time

import pytest

from src.domain.drive_chunker import chunk_text
from src.domain.drive_content_terms import content_terms
from src.domain.drive_text_gate import is_machine_text, measure

SEEDS = range(20)
_KINDS = ["COMPRA TARJ.", "PAGO MOVIL", "RECIBO", "TRANSFERENCIA"]


def _merchants(rng, n=200):
    return [" ".join("".join(rng.choice(string.ascii_uppercase) for _ in range(rng.randint(4, 9)))
                     for _ in range(rng.randint(1, 3))) for _ in range(n)]


def bank_statement(rng, transactions=1500):
    """A statement as a PDF parser prints it: one transaction per line, merchants Zipf-distributed."""
    merchants = _merchants(rng)
    weights = [1 / (i + 1) for i in range(len(merchants))]
    lines = ["Extracto de cuenta", "Titular: X", "Cuenta corriente"]
    balance = 2000.0
    for i in range(transactions):
        amount = -rng.uniform(1, 120)
        balance += amount
        lines.append(f"{i % 28 + 1:02d}/09/2026 {rng.choice(_KINDS)} {rng.choices(merchants, weights)[0]} "
                     f"{amount:.2f} {balance:.2f}")
    return "\n".join(lines)


# -- financial documents stay indexed (B-M5) ----------------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_bank_statement_with_zipf_merchants_passes(seed):
    assert not is_machine_text(bank_statement(random.Random(seed)))


@pytest.mark.parametrize("currency", ["€ {:,}", "{:,} EUR", "{:,} €", "${:,}"])
def test_balance_sheet_with_amounts_on_their_own_lines_passes(currency):
    rng = random.Random(7)
    accounts = ["Intangible assets", "Property, plant and equipment", "Right-of-use assets", "Inventories",
                "Investments in associates", "Deferred tax assets", "Trade receivables", "Other receivables",
                "Cash and cash equivalents", "Share capital", "Share premium", "Retained earnings", "Borrowings",
                "Lease liabilities", "Trade payables", "Tax payables", "Provisions", "Revenue", "Cost of sales",
                "Gross profit", "Distribution costs", "Administrative expenses", "Operating profit",
                "Finance income", "Finance costs", "Profit before tax", "Income tax", "Profit for the year"]
    lines = ["Annual report 2025", "Balance sheet"]
    for account in accounts:
        lines += [account, currency.format(rng.randint(1000, 999999)), currency.format(rng.randint(1000, 999999))]
    assert not is_machine_text("\n".join(lines))


def test_balance_sheet_dates_and_decimals_on_their_own_lines_are_not_near_empty():
    rng = random.Random(8)
    lines = []
    for i in range(40):
        lines += [f"Account line number {i}", f"€{rng.randint(10, 999)}", f"{rng.randint(1, 28):02d}.03.2025",
                  f"({rng.randint(1, 99)},{rng.randint(10, 99)})"]
    assert measure("\n".join(lines)).near_empty == 0.0


@pytest.mark.parametrize("seed", SEEDS)
def test_same_template_invoice_passes(seed):
    rng = random.Random(seed)
    lines = (["Invoice 2026-001", "Bill to: ACME"]
             + [f"Consulting services hourly rate - {rng.randint(1, 8)} h  {rng.randint(50, 90)}.00 EUR"
                for _ in range(30)]
             + ["Total 1234.00 EUR"])
    assert not is_machine_text("\n".join(lines))


# -- machine dumps still fail ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_telemetry_dump_is_machine_text(seed):
    rng = random.Random(seed)
    lines = [f"2026-10-01T12:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z device=sensor-{rng.randint(1, 12):02d} "
             f"temp={rng.uniform(15, 30):.2f} hum={rng.uniform(20, 80):.1f} batt={rng.randint(10, 100)} "
             f"rssi=-{rng.randint(40, 90)} status={rng.choice(['ok', 'ok', 'ok', 'low'])}"
             for _ in range(rng.randint(300, 1500))]
    assert is_machine_text("\n".join(lines))


@pytest.mark.parametrize("seed", SEEDS)
def test_long_server_log_is_machine_text(seed):
    rng = random.Random(seed)
    lines = [f"2026-10-01 12:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d},{rng.randint(0, 999):03d} "
             f"{rng.choice(['INFO', 'WARN', 'DEBUG'])} [worker-{rng.randint(1, 8)}] "
             f"{rng.choice(['GET', 'POST'])} /api/items/{rng.randint(1, 99999)} {rng.choice([200, 404, 500])} "
             f"{rng.randint(1, 900)}ms" for _ in range(rng.randint(300, 1500))]
    assert is_machine_text("\n".join(lines))


def test_sql_dump_with_prices_is_machine_text():
    rng = random.Random(3)
    text = "\n".join(f"INSERT INTO products (id, name, price, created) VALUES ({i}, 'product{i}', "
                     f"{rng.uniform(1, 500):.2f}, '2026-0{rng.randint(1, 9)}-{rng.randint(10, 28)}');"
                     for i in range(600))
    assert is_machine_text(text)


# -- linear in the text (B-M1) ---------------------------------------------------------------------------------------
# Wall-clock bounds on CPU-bound work: `timing`, so `make test-unit` runs them alone after the parallel suite
# (a saturated machine stretched them to 3-7 s there with no code change; alone each takes ~0.1 s).

def _timed(fn, *args):
    started = time.perf_counter()
    fn(*args)
    return time.perf_counter() - started


def _cjk(paragraph_chars, total_chars):
    rng = random.Random(0)
    han = [chr(c) for c in range(0x4E00, 0x4E00 + 3000)]
    paragraph = lambda: "".join(rng.choice(han) if i % 20 else "，" for i in range(paragraph_chars)) + "。"  # noqa: E731
    return "\n".join(paragraph() for _ in range(total_chars // paragraph_chars))


@pytest.mark.timing
@pytest.mark.parametrize("paragraph_chars", [5_000, 50_000])
def test_gate_is_linear_on_long_cjk_tokens(paragraph_chars):
    text = _cjk(paragraph_chars, 1_000_000)
    assert _timed(is_machine_text, text) < 3.0


@pytest.mark.timing
@pytest.mark.parametrize("text", ["a" * 2_000_000, "ACGT" * 500_000, "x\n" * 1_000_000, "word " * 400_000],
                         ids=["one-token", "one-dna-token", "short-lines", "one-line"])
def test_gate_is_linear_on_degenerate_text(text):
    assert _timed(is_machine_text, text) < 3.0


@pytest.mark.timing
def test_chunker_and_terms_are_fast_on_long_cjk_tokens():
    text = _cjk(50_000, 1_000_000)
    started = time.perf_counter()
    for chunk in chunk_text(text).chunks:
        content_terms(chunk.text)
    assert time.perf_counter() - started < 3.0
