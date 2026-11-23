"""`main()` forwards the graph's objects to the voice-resume, MCP and drive-connector builders (mutation survivors).

A `None` for any of these is silent at boot: the resume service would have no ticket store, the MCP components no user or
whitelist repository, and the drive connector no alert sink or link service.
"""
from typing import Any, Dict, List

import pytest

import main
from tests.unit.test_main_wiring_values import Captured, _instrumented_boot

pytestmark = pytest.mark.slow


@pytest.fixture
async def builds(monkeypatch, caplog) -> Dict[str, Any]:
    resume: List[Dict[str, Any]] = []
    links: List[Any] = []
    drive: List[Dict[str, Any]] = []
    resume_real = main.build_voice_resume_service
    links_real = main.build_drive_link_service
    drive_real = main.build_drive_connector_tools_from_container

    def resume_rec(**kwargs: Any) -> Any:
        resume.append(kwargs)
        return resume_real(**kwargs)

    def links_rec(**kwargs: Any) -> Any:
        built = links_real(**kwargs)
        links.append(built)
        return built

    async def drive_rec(container: Any, **kwargs: Any) -> Any:
        drive.append({"container": container, **kwargs})
        return await drive_real(container, **kwargs)

    monkeypatch.setattr(main, "build_voice_resume_service", resume_rec)
    monkeypatch.setattr(main, "build_drive_link_service", links_rec)
    monkeypatch.setattr(main, "build_drive_connector_tools_from_container", drive_rec)
    booted: Captured = await _instrumented_boot(monkeypatch, caplog)
    return {"booted": booted, "resume": resume, "links": links, "drive": drive}


async def test_the_voice_resume_service_gets_the_one_voice_ticket_store(builds):
    [(_, _, store)] = builds["booted"].stores
    [kwargs] = builds["resume"]

    assert store is not None
    assert kwargs["store"] is store


async def test_the_mcp_components_get_the_graphs_user_and_whitelist_repositories(builds):
    booted = builds["booted"]
    [(build_kwargs, _components)] = booted.mcp_builds

    assert booted.app.user_repo is not None
    assert booted.app.whitelist_repo is not None
    assert build_kwargs["user_repo"] is booted.app.user_repo
    assert build_kwargs["whitelist_repo"] is booted.app.whitelist_repo


async def test_the_drive_connector_tools_get_the_graphs_alert_sink(builds):
    booted = builds["booted"]
    [kwargs] = builds["drive"]

    assert booted.app.alert_sink is not None
    assert kwargs["alert_sink"] is booted.app.alert_sink


async def test_the_drive_connector_tools_get_the_built_drive_link_service(builds):
    [drive_links] = builds["links"]
    [kwargs] = builds["drive"]

    assert drive_links is not None
    assert kwargs["links"] is drive_links
