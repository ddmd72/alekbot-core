"""Extraction contract and the parser-first router (USER_DRIVE_INDEXING_RFC §9, §24.3). Pure."""
import pytest

from src.domain.drive_extraction import (
    EXTRACTOR_CHAINS, PARSER, READER, ExtractionOutcome, ExtractionRequest, ExtractionStatus, chain_for,
    garbage_share, model_route,
)
from src.domain.drive_index_params import MODEL_EXTRACT_DENSITY

S = ExtractionStatus
DENSE = "A clause of a contract, readable and plain. " * 20   # ≈ 900 characters


def out(status=S.TEXT, text="", pages=0):
    return ExtractionOutcome(status=status, text=text, pages_total=pages)


class TestChains:
    def test_pdf_parser_then_reader(self):
        assert chain_for("pdf") == (PARSER, READER)

    def test_image_goes_to_the_reader_only(self):
        assert chain_for("image") == (READER,)

    @pytest.mark.parametrize("kind", ["docx", "xlsx", "text", "eml", "rtf", "subtitles", "unknown"])
    def test_every_other_kind_is_parsed_only(self, kind):
        assert chain_for(kind) == (PARSER,)

    def test_table_names_only_known_extractors(self):
        assert {e for chain in EXTRACTOR_CHAINS.values() for e in chain} <= {PARSER, READER}


class TestModelRoute:
    """The four 'parser failed' signals of §9, and nothing else."""

    def test_no_text_layer(self):
        assert model_route(out(S.NO_TEXT, pages=3), "pdf") == "no_text"

    @pytest.mark.parametrize("status", [S.ERROR, S.UNSUPPORTED])
    def test_parser_error_gets_one_model_attempt(self, status):
        assert model_route(out(status), "pdf") == "parser_error"

    @pytest.mark.parametrize("status", [S.TIMEOUT, S.MEMORY, S.ENCRYPTED, S.TOO_LARGE, S.BUDGET_EXHAUSTED])
    def test_bombs_and_deterministic_outcomes_are_never_routed(self, status):
        assert model_route(out(status, pages=3), "pdf") is None

    def test_low_density(self):
        text = "x" * (MODEL_EXTRACT_DENSITY * 3 - 1)
        assert model_route(out(text=text, pages=3), "pdf") == "low_density"

    def test_density_at_the_threshold_is_not_routed(self):
        assert model_route(out(text="x" * (MODEL_EXTRACT_DENSITY * 3), pages=3), "pdf") is None

    def test_density_counts_text_without_surrounding_whitespace(self):
        text = "\n" * 5000 + "page 1"
        assert model_route(out(text=text, pages=1), "pdf") == "low_density"

    def test_unknown_page_count_skips_the_density_signal(self):
        assert model_route(out(text="short", pages=0), "pdf") is None

    def test_garbage_text(self):
        text = DENSE + "(cid:12)(cid:7)(cid:44)" * 10
        assert model_route(out(text=text, pages=1), "pdf") == "garbage_text"

    def test_clean_dense_text_stays_with_the_parser(self):
        assert model_route(out(text=DENSE, pages=1), "pdf") is None

    @pytest.mark.parametrize("kind", ["docx", "text", "xlsx"])
    def test_parser_only_kinds_are_never_routed(self, kind):
        assert model_route(out(S.NO_TEXT), kind) is None
        assert model_route(out(S.ERROR), kind) is None

    def test_a_kind_whose_first_link_is_the_reader_is_not_routed_again(self):
        assert model_route(out(S.NO_TEXT), "image") is None


class TestGarbageShare:
    def test_empty_and_whitespace(self):
        assert garbage_share("") == 0.0
        assert garbage_share("  \n\t") == 0.0

    def test_cid_runs(self):
        assert garbage_share("(cid:1)(cid:2)") == 1.0

    def test_replacement_characters(self):
        assert garbage_share("ab��") == 0.5

    def test_utf8_cyrillic_read_as_cp1251(self):
        mojibake = "Договор поставки товаров между сторонами".encode("utf-8").decode("cp1251", errors="replace")
        assert garbage_share(mojibake) > 0.3

    def test_utf8_cyrillic_read_as_latin1(self):
        mojibake = "Счёт на оплату".encode("utf-8").decode("latin-1")
        assert garbage_share(mojibake) > 0.3

    def test_utf8_latin_read_as_latin1(self):
        mojibake = "Café résumé, déjà vu".encode("utf-8").decode("latin-1")
        assert garbage_share(mojibake) > 0.05

    def test_real_cyrillic_capitals_are_not_garbage(self):
        # Р and С before Ukrainian І/Ї/Є or Russian Ё are real words, never mojibake
        text = "ПРОТОКОЛ РІК СІЧЕНЬ ВЕРЕСЕНЬ РЇ ПРЁТ ТАСС, 25°C, РОСІЯ «СВІТ» — ДОГОВІР № 12"
        assert garbage_share(text) == 0.0

    def test_share_is_capped_at_one(self):
        assert garbage_share("�") == 1.0


class TestContract:
    def test_request_defaults_to_no_model_pages(self):
        req = ExtractionRequest(path="/tmp/x", name="x.pdf", kind="pdf", mime_type="application/pdf",
                                user_id="u", account_id="a")
        assert req.max_model_pages == 0

    def test_outcome_defaults(self):
        o = ExtractionOutcome(status=S.TEXT)
        assert (o.text, o.pages_total, o.pages_sent, o.truncated, o.brief, o.extracted_by) == ("", 0, 0, False,
                                                                                                None, "")

    def test_status_values(self):
        assert {s.value for s in S} == {"text", "no_text", "unsupported", "too_large", "encrypted", "timeout",
                                        "memory", "error", "budget_exhausted"}
