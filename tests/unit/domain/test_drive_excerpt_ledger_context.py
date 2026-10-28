"""A18 across rounds: the reader sees each folder note and file line once; the orchestrator's block re-plans."""
from src.domain.drive_excerpt_context import ExcerptContext, folder_line
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_excerpt_folder_note import FolderNote
from src.domain.drive_excerpt_ledger import ExcerptLedger
from src.domain.drive_excerpt_render import render_excerpts
from src.domain.drive_index_params import EXCERPT_FOLDER_NOTES_CHARS_MAX
from tests.unit.domain.drive_excerpt_builders import LEASE, TOMS_PLAN, chunk, excerpt_file, nonces, result

RD = FolderNote("f-rd", "R&D", 1, "noted by the owner: «Acme specifications; the review process lives here»")
CONTRACTS = FolderNote("f-contracts", "Contracts", 1, "noted by the owner: «signed copies only»")
OWN = "noted by the owner: «Tom's draft, not final»"
PLAN_CTX = FileContext("item-1", TOMS_PLAN, ("f-rd",), "Tom Baker's plan", OWN)
MINUTES_CTX = FileContext("item-3", "R&D/minutes.md", ("f-rd",), "Review minutes", "")
LEASE_CTX = FileContext("item-2", f"Contracts/{LEASE}", ("f-contracts",), "Lease of Hanna's flat at Rua Imaginária 45, Porto", "")


def _file(ctx, *indexes):
    return excerpt_file(ctx.item_id, ctx.path, [chunk(ctx.item_id, i, f"Passage {i} of {ctx.path}.") for i in indexes])


def _round(ledger, ctxs, folders, *files):
    res = result(*files, context=ExcerptContext(files=tuple(ctxs), folders=folders))
    rendered = render_excerpts(res, nonce=nonces(), shown_folders=ledger.folders_shown(),
                               described_files=ledger.files_described())
    ledger.add(res, rendered.plan)
    return rendered.text


def test_each_line_prints_once_per_run_to_the_reader():
    ledger = ExcerptLedger()
    first = _round(ledger, [PLAN_CTX], {"f-rd": RD}, _file(PLAN_CTX, 0))
    second = _round(ledger, [PLAN_CTX, MINUTES_CTX], {"f-rd": RD}, _file(PLAN_CTX, 4), _file(MINUTES_CTX, 0))
    assert folder_line(RD) in first and OWN in first and "Tom Baker's plan" in first
    assert folder_line(RD) not in second and OWN not in second and "Tom Baker's plan" not in second
    assert "what it is (read from the file): Review minutes" in second


def test_a_folder_dropped_in_round_one_prints_in_round_two():
    big = FolderNote("f-rd", "R&D", 1, "noted by the owner: «" + "Acme " * (EXCERPT_FOLDER_NOTES_CHARS_MAX // 5) + "»")
    small = FolderNote("f-rd", "R&D", 1, RD.line)
    ledger = ExcerptLedger()
    first = _round(ledger, [PLAN_CTX], {"f-rd": big}, _file(PLAN_CTX, 0))
    assert 'Folder notes not shown (limit): "R&D"' in first
    assert "f-rd" not in ledger.folders_shown()
    second = _round(ledger, [MINUTES_CTX], {"f-rd": small}, _file(MINUTES_CTX, 0))
    assert folder_line(small) in second


def test_the_block_prints_each_folder_once_before_its_first_file():
    ledger = ExcerptLedger()
    _round(ledger, [PLAN_CTX], {"f-rd": RD}, _file(PLAN_CTX, 0))
    _round(ledger, [LEASE_CTX, MINUTES_CTX], {"f-rd": RD, "f-contracts": CONTRACTS},
           _file(LEASE_CTX, 1), _file(MINUTES_CTX, 2))
    _round(ledger, [PLAN_CTX], {"f-rd": RD}, _file(PLAN_CTX, 5))
    lines = ledger.block(nonce=nonces()).splitlines()
    assert lines.count(folder_line(RD)) == 1 and lines.count(folder_line(CONTRACTS)) == 1
    heads = [i for i, line in enumerate(lines) if line.startswith("[id ")]
    first_plan = next(i for i in heads if lines[i].startswith("[id drive:item-1:"))
    first_lease = next(i for i in heads if lines[i].startswith("[id drive:item-2:"))
    first_minutes = next(i for i in heads if lines[i].startswith("[id drive:item-3:"))
    assert lines.index(folder_line(RD)) == first_plan - 1
    assert lines.index(folder_line(CONTRACTS)) == first_lease - 1
    assert first_plan < first_lease < first_minutes
    # every file's own note and description reach the orchestrator, even those the reader saw in an earlier round
    assert OWN in lines
    assert sum("what it is (read from the file):" in line for line in lines) == 3


def test_the_block_holds_both_rounds_of_one_file_in_one_wrapper():
    ledger = ExcerptLedger()
    _round(ledger, [PLAN_CTX], {"f-rd": RD}, _file(PLAN_CTX, 0))
    _round(ledger, [PLAN_CTX], {"f-rd": RD}, _file(PLAN_CTX, 1))
    block = ledger.block(nonce=nonces())
    assert block.count(f'path="{TOMS_PLAN}"') == 1
    assert "[id drive:item-1:1-2 " in block
