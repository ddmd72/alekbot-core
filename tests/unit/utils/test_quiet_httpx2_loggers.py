"""google-genai 3.x sends through httpx2/httpcore2, which log every request at INFO: they are quieted like httpx."""
import logging

import pytest

from src.utils.logger import quiet_third_party_loggers


@pytest.mark.parametrize("name", ["httpx2", "httpcore2"])
def test_the_helper_sets_the_httpx2_family_to_warning(name):
    logging.getLogger(name).setLevel(logging.NOTSET)
    quiet_third_party_loggers()
    assert logging.getLogger(name).level == logging.WARNING


def test_an_httpx2_request_line_is_not_emitted(caplog):
    logging.getLogger("httpx2").setLevel(logging.NOTSET)
    quiet_third_party_loggers()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx2").info('HTTP Request: POST https://example.test/x "HTTP/1.1 200 OK"')
    assert "HTTP Request" not in caplog.text
