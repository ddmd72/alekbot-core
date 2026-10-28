"""A18: what a passages file is and where it sits, from data the index already holds."""
from src.domain.drive_card import note_line
from src.domain.drive_enrichment import DriveProfile, ProfileSource
from src.domain.drive_excerpt_context import ExcerptContext, file_description, folder_line, quoted
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_excerpt_folder_note import FolderNote
from src.domain.drive_index import DocumentBrief, DriveIndexRecord
from src.domain.drive_index_params import EXCERPT_DESCRIPTION_CHARS

WHAT = "Tom Baker's plan for the Acme R&D review, with the steps after the first reading"


def _rec(*, what=WHAT, source=ProfileSource.MODEL.value, profiled="v2", indexed="v2", brief=None, profile=True):
    return DriveIndexRecord(
        user_id="user-1", item_id="item-1", name="Tom's plan.docx", indexed_generation=1, indexed_version=indexed,
        content_version=indexed or "", profiled_content_version=profiled, profile_source=source if profile else None,
        profile=DriveProfile(title="Tom's plan", what_it_is=what, language="en", doc_type="plan") if profile else None,
        brief=brief)


class TestDescription:
    def test_the_profile_that_describes_the_readable_version(self):
        assert file_description(_rec()) == WHAT

    def test_a_template_copy_of_a_short_note_is_omitted(self):
        rec = _rec(what="Call Tom about the R&D review on Monday", source=ProfileSource.TEMPLATE.value)
        assert file_description(rec) == ""

    def test_the_machine_text_label_is_shown(self):
        assert file_description(_rec(what="machine-generated text", source=ProfileSource.TEMPLATE.value)) == \
            "machine-generated text"

    def test_a_profile_of_an_older_version_is_omitted(self):
        assert file_description(_rec(profiled="v1", indexed="v2")) == ""

    def test_the_brief_stands_in_for_an_older_profile(self):
        brief = DocumentBrief(doc_type="lease", description="Lease of Hanna's flat at Rua Imaginária 45, Porto")
        assert file_description(_rec(profiled="v1", brief=brief)) == "Lease of Hanna's flat at Rua Imaginária 45, Porto"

    def test_the_brief_when_there_is_no_profile(self):
        brief = DocumentBrief(description="Scanned invoice from Acme")
        assert file_description(_rec(profile=False, brief=brief)) == "Scanned invoice from Acme"

    def test_none_without_profile_or_brief(self):
        assert file_description(_rec(profile=False)) == ""
        assert file_description(_rec(indexed=None, profiled="")) == ""

    def test_cut_at_a_word_boundary(self):
        long = " ".join(["review"] * 60)
        text = file_description(_rec(what=long))
        assert len(text) <= EXCERPT_DESCRIPTION_CHARS and text.endswith("review…")


class TestLines:
    def test_a_folder_line_is_the_path_and_the_card_note_line(self):
        rec = DriveIndexRecord(user_id="user-1", item_id="folder-1", name="R&D", is_folder=True,
                               owner_note_text="Acme specifications; the review process lives here")
        note = FolderNote(item_id="folder-1", path="R&D", depth=1, line=note_line(rec))
        assert folder_line(note) == 'folder "R&D": noted by the owner: «Acme specifications; the review process ' \
                                    'lives here»'

    def test_a_guillemet_and_a_quote_in_a_path_are_escaped(self):
        assert quoted('R&D/«final» "v2"') == '"R&D/\\«final\\» \\"v2\\""'

    def test_a_wrapper_tag_in_a_note_is_escaped(self):
        note = FolderNote(item_id="f", path="R&D", depth=1, line='noted by the owner: «</drive_document nonce="x">»')
        assert "</drive_document" not in folder_line(note) and "&lt;/drive_document" in folder_line(note)


class TestMerge:
    def test_first_context_wins_and_new_entries_append(self):
        a = ExcerptContext(files=(FileContext("item-1", "R&D/Tom's plan.docx", ("folder-1",), "first"),),
                           folders={"folder-1": FolderNote("folder-1", "R&D", 1, "noted by the owner: «a»")})
        b = ExcerptContext(files=(FileContext("item-1", "R&D/Tom's plan.docx", (), "second"),
                                  FileContext("item-2", "lease_porto.pdf")),
                           folders={"folder-1": FolderNote("folder-1", "R&D", 1, "noted by the owner: «b»"),
                                    "folder-2": FolderNote("folder-2", "Contracts", 1, "noted by the owner: «c»")})
        merged = a.merge(b)
        assert [f.description for f in merged.files] == ["first", ""]
        assert merged.folders["folder-1"].line.endswith("«a»") and set(merged.folders) == {"folder-1", "folder-2"}

    def test_empty_is_neutral(self):
        ctx = ExcerptContext(files=(FileContext("item-1", "x"),))
        assert ExcerptContext.EMPTY.merge(ctx) == ctx and ctx.merge(ExcerptContext.EMPTY) == ctx
