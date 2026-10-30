"""One connector kind (MULTI_DRIVE_RFC §3, §4.2): the type only; the values live in
`composition/connector_registry.py`, and no OAuth parameter name lives here.

- `kind`: the stored credential kind; `integration`: the Cabinet group; `realm`: the OAuth edge that serves it.
- `required_scopes`: the scopes a consent must grant (granted scopes missing one are refused).
- `multiple`: whether a user may hold several credentials of this kind (cardinality is the connect service's policy,
  never the store's, E15).
- `id_space`: the prefix of the item and drive identities the kind's drive adapter builds (E7); "" for a kind with
  no drive items.
- `label`: the display name the Cabinet shows.
- `offered`: whether the Cabinet offers a connect button for the kind (False for a frozen integration whose stored
  credentials still work).
"""
from dataclasses import dataclass
from typing import Tuple

from .integration import Integration


@dataclass(frozen=True)
class ConnectorSpec:
    kind: str
    integration: Integration
    realm: str
    required_scopes: Tuple[str, ...]
    multiple: bool
    id_space: str
    label: str
    offered: bool = True
