"""
ServiceContainer wiring for skill files (AGENT_SKILLS_RFC §15).

Reuses the real-container fixture from test_service_container_wiring.py with a
GCS bucket configured, so the file-storage branch (and FileConversionService)
is built. The adapters are lazy, so a bucket name makes no network call.
"""

from unittest.mock import AsyncMock

import pytest

from src.services.skill_file_resolver import SkillFileResolver
from tests.unit.composition.test_service_container_wiring import (  # noqa: F401 — fixtures
    container,
    fake_config,
)


@pytest.fixture
def with_bucket(fake_config):  # noqa: F811 — the imported fixture, by name
    fake_config["GCS_MEDIA_BUCKET"] = "test-bucket"


@pytest.mark.usefixtures("with_bucket")
class TestSkillFilesWiring:
    def test_file_conversion_gets_a_skill_file_resolver(self, container):  # noqa: F811
        assert container.file_conversion_service is not None
        assert isinstance(container.file_conversion_service._skill_files, SkillFileResolver)

    def test_skill_service_shares_the_file_conversion_service(self, container):  # noqa: F811
        assert container.skill_service._file_conversion is container.file_conversion_service

    async def test_resolver_serves_a_system_skill_file(self, container):  # noqa: F811
        resolver = container.file_conversion_service._skill_files
        # No custom skill shadows the system one; the mocked Firestore client
        # cannot answer, so stub the one repository read the resolver makes.
        resolver._repo.get_current = AsyncMock(return_value=None)

        text = await resolver.read(
            "u1", "skill:domain-competency-research/references/final-manifest.md"
        )

        assert text.startswith("# Final manifest (on approval)")
        assert '"artifact_type": "Domain_Manifest"' in text
