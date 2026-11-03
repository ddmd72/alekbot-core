"""kind_of's last-resort branches: a non-Keynote `.key`, and audio/video told only by the declared mime."""
import pytest

from src.domain.document_kind import kind_of


def test_key_file_that_is_not_keynote_is_other():
    assert kind_of("deploy.key", "application/x-pem-file", is_folder=False) == "other"


@pytest.mark.parametrize("mime, kind", [("audio/x-unlisted", "audio"), ("video/x-unlisted", "video")])
def test_unknown_extension_falls_back_to_the_mime_family(mime, kind):
    assert kind_of("capture.unknownext", mime, is_folder=False) == kind
