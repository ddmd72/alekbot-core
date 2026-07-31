"""Regression tests for SlackResponseChannel.download_file and over-long filenames.

2026-07-31: a page saved from a browser reached Slack as a 255-character
`view-source_https___...ide.html`. `download_file` used it verbatim as the tempfile suffix,
the composed name came to 267 bytes, and the OS refused it with `[Errno 36] File name too
long`. The method swallowed the OSError, returned None, and ConversationHandler dropped the
attachment with only a warning — so the bot answered as if no file had been sent.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.slack.response_channel import SlackResponseChannel
from src.utils.file_conversion import NAME_MAX_BYTES

INCIDENT_FILENAME = (
    "view-source_https___www.correosaduanas.es_webauth_correosAduanas_private_"
    "tramitacionEnvioListado_tipoTramitacion=TRAMITACION_CORREOS&numEnvio=LT074295510GB"
    "&idEnvio=52992369&numEnvioDom=LT074295510GB&descEstadoTram=PENDIENTE+INSPECCION"
    "+PARADUANERA&ide.html"
)


@pytest.fixture
def response_channel():
    return SlackResponseChannel(
        app_client=AsyncMock(), channel_id="C123", bot_token="xoxb-test"
    )


def _mock_session(mock_session_class, chunks=(b"<html>", b"")):
    response = MagicMock()
    response.status = 200
    response.content.read = AsyncMock(side_effect=list(chunks))
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=response)
    mock_session_class.return_value = session


class TestDownloadFileNameLength:

    @pytest.mark.asyncio
    @patch("aiohttp.ClientSession")
    async def test_over_long_filename_still_downloads(self, mock_session_class, response_channel):
        _mock_session(mock_session_class)

        path = await response_channel.download_file(
            f"https://files.slack.com/files-pri/T1-F1/{INCIDENT_FILENAME}", "text/html"
        )

        assert path is not None, "the incident: errno 36 was swallowed and None returned"
        try:
            assert len(os.path.basename(path).encode("utf-8")) <= NAME_MAX_BYTES
            with open(path, "rb") as f:
                assert f.read() == b"<html>", "content must survive the rename"
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    @patch("aiohttp.ClientSession")
    async def test_cyrillic_filename_is_bounded_by_bytes(self, mock_session_class, response_channel):
        """2 bytes per character: a 200-character Cyrillic name is 400 bytes."""
        _mock_session(mock_session_class)

        path = await response_channel.download_file(
            "https://files.slack.com/files-pri/T1-F1/" + "отчёт" * 40 + ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        assert path is not None
        try:
            assert len(os.path.basename(path).encode("utf-8")) <= NAME_MAX_BYTES
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    @patch("aiohttp.ClientSession")
    async def test_ordinary_filename_stays_recognisable(self, mock_session_class, response_channel):
        """Truncation must not cost readability for the normal case."""
        _mock_session(mock_session_class)

        path = await response_channel.download_file(
            "https://files.slack.com/files-pri/T1-F1/report.pdf", "application/pdf"
        )

        try:
            assert path.endswith("_report.pdf")
        finally:
            os.unlink(path)
