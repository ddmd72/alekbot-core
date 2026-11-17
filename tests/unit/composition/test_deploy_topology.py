"""`build_deploy_topology`: the one derivation of the service URL, queue suffix and regions."""
from unittest.mock import MagicMock

import pytest

from src.composition.deploy_topology import DeployTopology, build_deploy_topology


def _env(is_development: bool) -> MagicMock:
    env = MagicMock()
    env.is_development = is_development
    return env


def test_local_settings_fall_back_to_localhost_and_the_dev_suffix():
    topology = build_deploy_topology({}, _env(True))

    assert topology == DeployTopology(service_url="http://localhost:8080", queue_suffix="dev",
                                      region="us-central1", slack_intake_region="europe-west1")
    assert topology.cabinet_memory_url == "http://localhost:8080/cabinet?tab=memory"


def test_deployed_settings_use_their_url_and_the_prod_suffix():
    topology = build_deploy_topology({"CLOUD_RUN_SERVICE_URL": "https://main.example.test"}, _env(False))

    assert topology == DeployTopology(service_url="https://main.example.test", queue_suffix="prod",
                                      region="us-central1", slack_intake_region="europe-west1")
    assert topology.cabinet_memory_url == "https://main.example.test/cabinet?tab=memory"


def test_an_empty_url_is_the_same_as_none():
    assert build_deploy_topology({"CLOUD_RUN_SERVICE_URL": ""}, _env(True)).service_url == "http://localhost:8080"


def test_the_topology_is_immutable():
    topology = build_deploy_topology({}, _env(True))
    with pytest.raises(AttributeError):
        topology.queue_suffix = "prod"  # type: ignore[misc]
