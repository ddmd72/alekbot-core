"""A18 / OQ-A3: the folder-note cap counts folder lines only, drops them whole, farthest first."""
from src.domain.drive_excerpt_context import folder_line, plan_context
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_excerpt_folder_note import FolderNote


def _note(item_id, path, depth, words=3):
    return FolderNote(item_id=item_id, path=path, depth=depth,
                      line="noted by the owner: «" + " ".join(["Acme"] * words) + "»")


# R&D (1) / Specs (2) / Tom's plan.docx ;  R&D (1) / Reviews (2) / 2026 (3) / minutes.md ;  Contracts (1) / lease
RD = _note("f-rd", "R&D", 1)
SPECS = _note("f-specs", "R&D/Specs", 2)
REVIEWS = _note("f-reviews", "R&D/Reviews", 2)
Y2026 = _note("f-2026", "R&D/Reviews/2026", 3)
CONTRACTS = _note("f-contracts", "Contracts", 1)
FOLDERS = {n.item_id: n for n in (RD, SPECS, REVIEWS, Y2026, CONTRACTS)}

PLAN = FileContext("item-1", "R&D/Specs/Tom's plan.docx", ("f-specs", "f-rd"), "Tom's plan", "")
MINUTES = FileContext("item-2", "R&D/Reviews/2026/minutes.md", ("f-2026", "f-reviews", "f-rd"), "", "")
LEASE = FileContext("item-3", "Contracts/lease_porto.pdf", ("f-contracts",), "", "noted by the owner: «Hanna's lease»")


def _len(*notes):
    return sum(len(folder_line(n)) for n in notes)


def _plan(files=(PLAN, MINUTES, LEASE), *, chars_max=10_000, shown=frozenset(), described=frozenset(),
          folders=FOLDERS):
    return plan_context(files, folders, shown_folders=shown, described_files=described, chars_max=chars_max)


def test_everything_fits_each_folder_before_its_first_file_root_first():
    plan = _plan()
    assert [n.item_id for n in plan.before["item-1"]] == ["f-rd", "f-specs"]
    assert [n.item_id for n in plan.before["item-2"]] == ["f-reviews", "f-2026"]      # R&D printed already
    assert [n.item_id for n in plan.before["item-3"]] == ["f-contracts"]
    assert plan.dropped == ()
    assert plan.shown_after == set(FOLDERS)


def test_farthest_first_across_two_branches():
    # distances: Specs 1, 2026 1, Contracts 1, Reviews 2, R&D 2 (its smallest, via Specs)
    everything = _len(*FOLDERS.values())
    plan = _plan(chars_max=everything - 1)
    assert plan.dropped == ("R&D",)                   # dist 2, depth 1 beats Reviews (dist 2, depth 2)
    plan = _plan(chars_max=everything - _len(RD) - 1)
    assert plan.dropped == ("R&D", "R&D/Reviews")


def test_a_shared_folder_takes_its_smallest_distance():
    # R&D is 3 levels above the minutes but 1 above a file right in it: at 1 it outlasts Reviews (2)
    top = FileContext("item-4", "R&D/Tom's plan.docx", ("f-rd",))
    plan = _plan((MINUTES, top), chars_max=_len(RD, REVIEWS, Y2026) - 1)
    assert plan.dropped == ("R&D/Reviews",)
    plan = _plan((MINUTES,), chars_max=_len(RD, REVIEWS, Y2026) - 1)
    assert plan.dropped == ("R&D",)


def test_the_tie_order_is_depth_then_path():
    a = _note("f-a", "Archive", 1)
    b = _note("f-b", "Contracts", 1)
    files = (FileContext("i1", "Archive/x.pdf", ("f-a",)), FileContext("i2", "Contracts/y.pdf", ("f-b",)))
    plan = _plan(files, folders={"f-a": a, "f-b": b}, chars_max=_len(b))
    assert plan.dropped == ("Archive",)


def test_a_note_is_dropped_whole_never_cut():
    plan = _plan((PLAN,), chars_max=_len(RD, SPECS) - 1)
    assert plan.dropped == ("R&D",)
    assert plan.before["item-1"] == (SPECS,)


def test_own_notes_and_descriptions_are_never_dropped():
    plan = _plan(chars_max=0)
    assert set(plan.dropped) == {n.path for n in FOLDERS.values()}
    assert plan.before == {}
    assert plan.described == {"item-1", "item-2", "item-3"}
    assert plan.has_owner_line is True                          # LEASE's own note still prints


def test_un_noted_folders_are_silent():
    files = (FileContext("item-1", "R&D/Specs/Tom's plan.docx", ("f-specs", "f-rd")),)
    plan = _plan(files, folders={"f-rd": RD, "f-specs": FolderNote("f-specs", "R&D/Specs", 2, "")})
    assert plan.before == {"item-1": (RD,)}
    plan = _plan(files, folders={})
    assert plan.before == {} and plan.has_owner_line is False


def test_shown_folders_and_described_files_suppress_repeats():
    plan = _plan(shown=frozenset({"f-rd", "f-specs"}), described=frozenset({"item-1"}))
    assert "item-1" not in plan.before
    assert [n.item_id for n in plan.before["item-2"]] == ["f-reviews", "f-2026"]
    assert plan.described == {"item-2", "item-3"}
    assert plan.shown_after == set(FOLDERS)


def test_a_dropped_folder_is_not_marked_shown():
    plan = _plan((PLAN,), chars_max=_len(SPECS))
    assert "f-rd" not in plan.shown_after and "f-specs" in plan.shown_after


def test_file_lines_never_count_against_the_budget():
    long_note = "noted by the owner: «" + "Acme " * 120 + "»"
    files = tuple(FileContext(f"item-{i}", f"R&D/Specs/plan {i}.docx", ("f-specs", "f-rd"), "x" * 200, long_note)
                  for i in range(20))
    plan = _plan(files, chars_max=_len(RD, SPECS))
    assert plan.dropped == ()
    assert plan.before["item-0"] == (RD, SPECS)


def test_has_owner_line_only_when_something_owner_written_prints():
    no_notes = (FileContext("item-9", "Photos/Coimbra 2024/a.jpg"),)
    assert _plan(no_notes).has_owner_line is False
    assert _plan((PLAN,)).has_owner_line is True                 # folder lines
    assert _plan((LEASE,), shown=frozenset({"f-contracts"})).has_owner_line is True   # own note
    assert _plan((LEASE,), shown=frozenset({"f-contracts"}), described=frozenset({"item-3"})).has_owner_line is False


def test_a_file_given_twice_is_planned_once():
    plan = _plan((PLAN, PLAN))
    assert list(plan.before) == ["item-1"] and plan.described == {"item-1"}
