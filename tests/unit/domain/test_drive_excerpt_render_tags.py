"""Page lines, the repeated-ids line and the context plan are keyed by (ref tag, item id): the same item id on two
connections never shares them (multi-drive 2b P-C)."""
from dataclasses import replace

from src.domain.drive_excerpt_context import ExcerptContext
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_excerpt_folder_note import FolderNote
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_render import context_key, render_block, render_excerpts
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result


def _file(tag, path):
    return replace(excerpt_file("item-1", path, [chunk("item-1", 0, f"Text of {path}.")], ref_tag=tag), ref_tag=tag)


def _span(path):
    return PageSpan(first=1, last=1, total=3, version=1, next=2, end=False, truncated=False, stale=False, path=path)


def test_a_page_read_on_one_connection_is_not_printed_for_the_other():
    files = (_file("a1", "Drive/plan.docx"), _file("b2", "Acme/plan.docx"))
    text = render_block(files, ExcerptContext.EMPTY, nonce=nonces(), pages={("a1", "item-1", 1): [_span("Drive/plan.docx")]})
    assert text.count("pages read:") == 1
    assert text.index("pages read:") < text.index("Text of Drive/plan.docx.") < text.index("Text of Acme/plan.docx.")


def test_the_repeated_line_names_each_chunk_under_its_own_tag():
    res = result(repeated=(("item-1", 1, 0), ("item-1", 1, 1)), repeated_tags=("a1", "b2"))
    text = render_excerpts(res, nonce=nonces()).text
    assert "drive:a1:item-1:1" in text and "drive:b2:item-1:2" in text


def test_context_is_planned_per_connection():
    files = (_file("a1", "Drive/plan.docx"), _file("b2", "Acme/plan.docx"))
    context = ExcerptContext(
        files=tuple(FileContext(item_id=context_key(t, "item-1"), path=p, ancestors=(context_key(t, "F1"),),
                                description=f"About {p}") for t, p in (("a1", "Drive/plan.docx"), ("b2", "Acme/plan.docx"))),
        folders={context_key(t, "F1"): FolderNote(item_id=context_key(t, "F1"), path=f, depth=1, line="noted")
                 for t, f in (("a1", "Drive/R&D"), ("b2", "Acme/R&D"))})
    text = render_block(files, context, nonce=nonces())
    assert 'folder "Drive/R&D"' in text and 'folder "Acme/R&D"' in text
    assert "About Drive/plan.docx" in text and "About Acme/plan.docx" in text
