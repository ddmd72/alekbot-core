"""The `page_next` seam of the passages render (drive connector, drift D5): the default prints today's PAGE_NEXT line,
a custom template replaces that one line and nothing else; the end lines never take it."""
from src.domain import drive_excerpt_render as R
from src.domain.drive_excerpt_context import quoted
from src.domain.drive_excerpt_page import PageSpan
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, nonces, result

PATH = "R&D/Tom's plan.docx"
CUSTOM = "More of {path}: passages {first}-{last} of {total} (v{version}); call again with from={next}."


def _span(**kw):
    base = dict(first=1, last=10, total=30, version=2, next=11, path=PATH, ref="drive:item-1")
    base.update(kw)
    return PageSpan(**base)


def _page_result(span):
    file = excerpt_file("item-1", PATH, [chunk("item-1", i, f"Step {i + 1}. Invoice FA-0137 is paid.") for i in (0, 1)])
    return result(file, request=ExcerptRangeRequest("drive:item-1", 1, 10, page=True), page=span)


class TestPageLine:
    def test_the_default_is_page_next(self):
        span = _span()
        assert R.page_line(span) == R.PAGE_NEXT.format(first=1, last=10, total=30, path=quoted(PATH), version=2,
                                                       next=11)
        assert R.page_line(span, page_next=R.PAGE_NEXT) == R.page_line(span)

    def test_a_custom_template_fills_the_same_numbers(self):
        line = R.page_line(_span(), page_next=CUSTOM)
        assert line == f"More of {quoted(PATH)}: passages 1-10 of 30 (v2); call again with from=11."

    def test_the_stale_suffix_follows_a_custom_template(self):
        assert R.page_line(_span(stale=True), page_next=CUSTOM).endswith(R.PAGE_STALE)

    def test_the_end_lines_ignore_the_template(self):
        end = _span(first=21, last=30, next=None, end=True)
        cut = _span(first=21, last=30, next=None, end=True, truncated=True)
        assert R.page_line(end, page_next=CUSTOM) == R.page_line(end)
        assert R.page_line(cut, page_next=CUSTOM) == R.page_line(cut)


class TestRenderExcerpts:
    def test_the_default_render_is_unchanged(self):
        res = _page_result(_span())
        plain = R.render_excerpts(res, nonce=nonces())
        explicit = R.render_excerpts(res, nonce=nonces(), page_next=R.PAGE_NEXT)
        assert plain == explicit
        assert plain.text.splitlines()[-1] == R.page_line(_span())

    def test_a_custom_template_replaces_only_the_page_line(self):
        res = _page_result(_span())
        plain = R.render_excerpts(res, nonce=nonces()).text.splitlines()
        custom = R.render_excerpts(res, nonce=nonces(), page_next=CUSTOM).text.splitlines()
        assert custom[:-1] == plain[:-1]
        assert custom[-1] == R.page_line(_span(), page_next=CUSTOM) != plain[-1]

    def test_the_plan_is_the_same_under_a_custom_template(self):
        res = _page_result(_span())
        assert (R.render_excerpts(res, nonce=nonces(), page_next=CUSTOM).plan
                == R.render_excerpts(res, nonce=nonces()).plan)

    def test_the_end_of_the_file_ignores_the_template(self):
        res = _page_result(_span(first=21, last=30, next=None, end=True))
        assert R.render_excerpts(res, nonce=nonces(), page_next=CUSTOM) == R.render_excerpts(res, nonce=nonces())
