"""ConnectorRegistry — every connector kind, its spec and its realm's OAuth edge (MULTI_DRIVE_RFC §3, §4.2, T-6).

The kind literals live here and in the adapters only: a service receives its `ConnectorSpec` by constructor from this
registry and filters credentials by `spec.kind`, never by a literal or by integration (E25). `required_scopes` are the
service scopes each connect asks for today (Google checks them at consent, `google_oauth_service.py`; Microsoft
requests them in `oauth_app.py` and checks none yet); `label` is the display name the Cabinet shows.

Public interface:
    ConnectorRegistry(edges: Mapping[str, OAuthProviderPort] = {})
    get(kind) -> ConnectorSpec                         unknown kind raises KeyError
    kinds(integration) -> List[str]                    in declaration order
    realm_edge(realm) -> OAuthProviderPort             unknown realm raises KeyError
"""
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional

from ..domain.connector_spec import ConnectorSpec
from ..domain.integration import Integration
from ..ports.oauth_provider_port import OAuthProviderPort

GMAIL = "gmail"
GOOGLE_TASKS = "google_tasks"
MICROSOFT_TODO = "microsoft_todo"
MICROSOFT_ONEDRIVE = "microsoft_onedrive"

_GOOGLE = "google"
_MICROSOFT = "microsoft"

_SPECS = (
    ConnectorSpec(kind=GMAIL, integration=Integration.MAIL, realm=_GOOGLE,
                  required_scopes=("https://www.googleapis.com/auth/gmail.readonly",), multiple=False,
                  id_space="", label="Gmail"),
    ConnectorSpec(kind=GOOGLE_TASKS, integration=Integration.TASKS, realm=_GOOGLE,
                  required_scopes=("https://www.googleapis.com/auth/tasks",), multiple=False,
                  id_space="", label="Google Tasks"),
    ConnectorSpec(kind=MICROSOFT_TODO, integration=Integration.TASKS, realm=_MICROSOFT,
                  required_scopes=("Tasks.ReadWrite",), multiple=False,
                  id_space="", label="Microsoft To Do"),
    # multiple=False until slice 1b brings the drive connection: a consent of another account replaces the drive
    # credential exactly as today; 1b flips it to True.
    ConnectorSpec(kind=MICROSOFT_ONEDRIVE, integration=Integration.DRIVE, realm=_MICROSOFT,
                  required_scopes=("Files.ReadWrite.AppFolder",), multiple=False,
                  id_space=MICROSOFT_ONEDRIVE, label="OneDrive"),
)


class ConnectorRegistry:
    """kind → spec, and realm → OAuth edge. The edges are passed in (composition builds them); an empty mapping is
    allowed until the edges are wired."""

    def __init__(self, edges: Optional[Mapping[str, OAuthProviderPort]] = None) -> None:
        self._specs: Dict[str, ConnectorSpec] = {spec.kind: spec for spec in _SPECS}
        self._edges: Mapping[str, OAuthProviderPort] = MappingProxyType(dict(edges or {}))

    def get(self, kind: str) -> ConnectorSpec:
        try:
            return self._specs[kind]
        except KeyError:
            raise KeyError(f"Unknown connector kind: {kind!r}") from None

    def kinds(self, integration: Integration) -> List[str]:
        return [spec.kind for spec in self._specs.values() if spec.integration is integration]

    def realm_edge(self, realm: str) -> OAuthProviderPort:
        try:
            return self._edges[realm]
        except KeyError:
            raise KeyError(f"No OAuth edge for realm: {realm!r}") from None
