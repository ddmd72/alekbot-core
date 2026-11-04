"""build_sandbox_media (S5 design §2.6, §2.7): the MediaAudioPort over the light service's tunnel, signed for the light URL
as its audience, drive reads through HttpRangeSource; an empty URL is refused with an ERROR; no knob, no environment."""
import ast
import logging
from pathlib import Path

import pytest

from src.adapters.sandbox_client.media_range_source import HttpRangeSource
from src.adapters.sandbox_client.metadata_id_token_provider import MetadataIdTokenProvider
from src.adapters.sandbox_client.sandbox_media_adapter import SandboxMediaAdapter
from src.adapters.sandbox_client.sandbox_tunnel_client import SandboxTunnelClient
from src.composition import sandbox_media_setup
from src.composition.sandbox_media_setup import build_sandbox_media
from src.domain.drive_media_source import MediaSource
from src.ports.media_audio_port import MediaAudioPort


def test_the_port_over_the_tunnel_with_the_light_url_as_audience():
    media = build_sandbox_media("https://light.example.com/")
    assert isinstance(media, SandboxMediaAdapter) and isinstance(media, MediaAudioPort)
    client = media._client
    assert isinstance(client, SandboxTunnelClient) and client.base_url == "https://light.example.com"
    provider = client._token_provider
    assert isinstance(provider, MetadataIdTokenProvider) and provider.audience == "https://light.example.com"


def test_drive_reads_go_through_the_range_source():
    media = build_sandbox_media("https://light.example.com")
    source = media._range_source(MediaSource(url="https://www.example.com/r", headers={"Authorization": "Bearer tok-x"}),
                                 240.0)
    assert isinstance(source, HttpRangeSource)


@pytest.mark.parametrize("url", ["", "   ", None])
def test_an_empty_url_is_refused_with_an_error_line(url, caplog):
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
        build_sandbox_media(url)  # type: ignore[arg-type]
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_the_setup_reads_no_environment():
    tree = ast.parse(Path(sandbox_media_setup.__file__).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree)
                                                                          if isinstance(n, ast.Attribute)}
    assert not names & {"environ", "getenv", "os"}
