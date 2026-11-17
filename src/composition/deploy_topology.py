"""Where the main service runs: its own URL, queue suffix and regions, derived once from the settings."""
from dataclasses import dataclass
from typing import Mapping, Any

from ..config.environment import EnvironmentConfig

_LOCAL_SERVICE_URL = "http://localhost:8080"
_REGION = "us-central1"
_SLACK_INTAKE_REGION = "europe-west1"


@dataclass(frozen=True)
class DeployTopology:
    """The deploy literals of the main service. Everything that names the service's URL, its Cloud Tasks queues or the
    region of its Cloud Run jobs reads them here, so no module derives them again."""

    service_url: str
    queue_suffix: str
    region: str
    slack_intake_region: str

    @property
    def cabinet_memory_url(self) -> str:
        return f"{self.service_url}/cabinet?tab=memory"


def build_deploy_topology(config: Mapping[str, Any], env_config: EnvironmentConfig) -> DeployTopology:
    """The one reader of `CLOUD_RUN_SERVICE_URL` and of the development flag for queue and job names."""
    return DeployTopology(
        service_url=config.get("CLOUD_RUN_SERVICE_URL") or _LOCAL_SERVICE_URL,
        queue_suffix="dev" if env_config.is_development else "prod",
        region=_REGION,
        slack_intake_region=_SLACK_INTAKE_REGION,
    )
