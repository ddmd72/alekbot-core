"""ConnectorRegistry — every connector kind, its spec and its realm's OAuth edge (MULTI_DRIVE_RFC §3, §4.2, T-6).

The kind literals live here and in the adapters only: a service receives its `ConnectorSpec` by constructor from this
registry and filters credentials by `spec.kind`, never by a literal or by integration (E25). `required_scopes` are the
service scopes each connect asks for (the realm's edge adds its identity scopes; `OAuthConnectService` refuses a grant
that misses one, for every realm); `label` is the display name the Cabinet shows; `offered` False keeps a frozen kind
(Google Tasks) out of the Cabinet's connect buttons.

Drive kinds (MULTI_DRIVE_RFC §6, §7.2, E7): each DRIVE spec declares a non-empty `id_space`, unique across the
registry and free of `:`, the prefix of every item and drive identity its adapter builds; its drive adapter is passed
in by kind (`drive_adapters`) and the composition builds `DriveProviderRouter(registry.drive_adapters())` from them, so
a new drive kind is one spec plus one adapter here. `specs` replaces the four production specs (tests register fake
kinds that way).

Public interface:
    ConnectorRegistry(edges: Mapping[str, OAuthProviderPort] = {}, drive_adapters: Mapping[str, UserDrivePort] = {},
                      specs: Sequence[ConnectorSpec] = the four)     ValueError for an inconsistent registry
    get(kind) -> ConnectorSpec                         unknown kind raises KeyError
    kinds(integration) -> List[str]                    in declaration order
    realm_edge(realm) -> OAuthProviderPort             unknown realm raises KeyError
    specs() -> Mapping[str, ConnectorSpec]             kind -> spec, read-only
    edges() -> Mapping[str, OAuthProviderPort]         realm -> edge, read-only
    offered() -> List[ConnectorSpec]                   the kinds the Cabinet offers, in declaration order
    drive_adapter(kind) -> UserDrivePort               unknown or adapter-less kind raises KeyError
    drive_adapters() -> Mapping[str, UserDrivePort]    kind -> drive adapter, read-only
"""
from types import MappingProxyType
from typing import List, Mapping, Optional, Sequence

from ..domain.connector_spec import ConnectorSpec
from ..domain.integration import Integration
from ..ports.oauth_provider_port import OAuthProviderPort
from ..ports.user_drive_port import UserDrivePort

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
                  id_space="", label="Google Tasks", offered=False),   # frozen: stored grants work, no new connects
    ConnectorSpec(kind=MICROSOFT_TODO, integration=Integration.TASKS, realm=_MICROSOFT,
                  required_scopes=("Tasks.ReadWrite",), multiple=False,
                  id_space="", label="Microsoft To Do"),
    # multiple=True (slice 1b, E15, E27): a consent of another account is another credential and another drive
    # connection; a consent of the same account keeps its credential and reconnects its connection.
    ConnectorSpec(kind=MICROSOFT_ONEDRIVE, integration=Integration.DRIVE, realm=_MICROSOFT,
                  required_scopes=("Files.ReadWrite.AppFolder",), multiple=True,
                  id_space=MICROSOFT_ONEDRIVE, label="OneDrive"),
)


class ConnectorRegistry:
    """kind → spec, and realm → OAuth edge. The edges are passed in (composition builds them); an empty mapping is
    allowed until the edges are wired."""

    def __init__(self, edges: Optional[Mapping[str, OAuthProviderPort]] = None,
                 drive_adapters: Optional[Mapping[str, UserDrivePort]] = None,
                 specs: Optional[Sequence[ConnectorSpec]] = None) -> None:
        chosen = tuple(_SPECS if specs is None else specs)
        by_kind = {spec.kind: spec for spec in chosen}
        if len(by_kind) != len(chosen):
            raise ValueError("two connector specs share a kind")
        _check_id_spaces(chosen)
        for kind in (drive_adapters or {}):
            spec = by_kind.get(kind)
            if spec is None or spec.integration is not Integration.DRIVE:
                raise ValueError(f"a drive adapter needs a DRIVE spec of its kind: {kind!r}")
        self._specs: Mapping[str, ConnectorSpec] = MappingProxyType(by_kind)
        self._edges: Mapping[str, OAuthProviderPort] = MappingProxyType(dict(edges or {}))
        self._drives: Mapping[str, UserDrivePort] = MappingProxyType(dict(drive_adapters or {}))

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

    def specs(self) -> Mapping[str, ConnectorSpec]:
        return self._specs

    def edges(self) -> Mapping[str, OAuthProviderPort]:
        return self._edges

    def offered(self) -> List[ConnectorSpec]:
        return [spec for spec in self._specs.values() if spec.offered]

    def drive_adapter(self, kind: str) -> UserDrivePort:
        try:
            return self._drives[kind]
        except KeyError:
            raise KeyError(f"No drive adapter for kind: {kind!r}") from None

    def drive_adapters(self) -> Mapping[str, UserDrivePort]:
        return self._drives


def _check_id_spaces(specs: Sequence[ConnectorSpec]) -> None:
    """Every DRIVE spec has a non-empty `id_space` without `:`, and no two specs share one (E7: identities are unique
    across providers because their prefixes are)."""
    seen: List[str] = []
    for spec in specs:
        if spec.integration is Integration.DRIVE and (not spec.id_space or ":" in spec.id_space):
            raise ValueError(f"a drive kind needs an id_space without ':': {spec.kind!r}")
        if spec.id_space:
            if spec.id_space in seen:
                raise ValueError(f"two connector specs share the id_space {spec.id_space!r}")
            seen.append(spec.id_space)

