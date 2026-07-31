"""Unit tests for `safe_temp_suffix` (src/utils/file_conversion.py).

Regression origin: 2026-07-31, a page saved from a browser arrived on Slack as
`view-source_https___www.correosaduanas.es_...ide.html` (255 chars). Both response channels
fed that straight into `tempfile.NamedTemporaryFile(suffix=f"_{filename}")`, which produced a
267-byte name and `[Errno 36] File name too long` — NAME_MAX is 255 BYTES on ext4/overlayfs.
The download returned None and the attachment was silently dropped.

The unit is byte-oriented on purpose: a Cyrillic filename costs 2 bytes per character in UTF-8,
so a character-based limit would still overflow at ~130 visible characters.
"""

import tempfile

import pytest

from src.utils.file_conversion import NAME_MAX_BYTES, safe_temp_suffix


def _name_bytes(suffix: str) -> int:
    """Bytes tempfile will actually spend: its own prefix + random stem + our suffix."""
    return len(tempfile.gettempprefix()) + 8 + len(suffix.encode("utf-8"))


class TestFitsWithinNameMax:

    def test_the_incident_filename_now_fits(self):
        filename = (
            "view-source_https___www.correosaduanas.es_webauth_correosAduanas_private_"
            "tramitacionEnvioListado_tipoTramitacion=TRAMITACION_CORREOS&numEnvio=LT074295510GB"
            "&idEnvio=52992369&numEnvioDom=LT074295510GB&descEstadoTram=PENDIENTE+INSPECCION"
            "+PARADUANERA&ide.html"
        )
        # 255 chars on its own — it is tempfile's own 12-byte prefix that tips it over, which
        # is why the naive `suffix=f"_{filename}"` produced a 267-byte name.
        assert _name_bytes(f"_{filename}") == 267, "fixture must reproduce the overflow"
        assert _name_bytes(safe_temp_suffix(filename)) <= NAME_MAX_BYTES

    @pytest.mark.parametrize("filename", [
        "a" * 300 + ".html",
        "документ" * 40 + ".docx",          # 2 bytes/char — overflows at ~130 chars
        "🧾" * 100 + ".pdf",                 # 4 bytes/char
        "x" * 400,                           # no extension at all
        "report." + "e" * 300,               # pathological extension
    ])
    def test_never_exceeds_the_limit(self, filename):
        assert _name_bytes(safe_temp_suffix(filename)) <= NAME_MAX_BYTES

    def test_the_result_is_actually_creatable(self):
        """The real check: the OS accepts it. This is the call that raised errno 36."""
        with tempfile.NamedTemporaryFile(suffix=safe_temp_suffix("ы" * 400 + ".html")) as tmp:
            assert tmp.name


class TestKeepsWhatIsUseful:

    def test_short_names_pass_through_untouched(self):
        assert safe_temp_suffix("report.pdf") == "_report.pdf"

    def test_extension_survives_truncation(self):
        assert safe_temp_suffix("a" * 300 + ".html").endswith(".html")

    def test_the_start_of_the_name_is_what_is_kept(self):
        """Truncate the tail: the leading part is what identifies the file to a human."""
        assert safe_temp_suffix("invoice_" + "x" * 300 + ".pdf").startswith("_invoice_")

    def test_multibyte_truncation_does_not_split_a_character(self):
        suffix = safe_temp_suffix("документ" * 40 + ".docx")
        suffix.encode("utf-8").decode("utf-8")  # raises if a character was cut in half
        assert "�" not in suffix

    def test_an_unusable_extension_is_dropped_rather_than_eating_the_budget(self):
        suffix = safe_temp_suffix("report." + "e" * 300)
        assert suffix.startswith("_report")

    def test_empty_filename_is_tolerated(self):
        assert safe_temp_suffix("") == ""
