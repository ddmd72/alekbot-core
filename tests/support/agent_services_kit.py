"""`UserAgentFactory` keyword arguments from one flat dict, for tests that build the factory from mocks.

The factory takes the container's shared services as one `AgentServices`; these tests keep a flat dict of mocks (a
fixture, plus per-test overrides). `user_agent_factory_kwargs` moves every `AgentServices` field out of that dict into
`services=`. A field the test does not give is `None`, except `drive_notes_enabled`, which keeps the factory's former
default `True`.
"""
from dataclasses import fields
from typing import Any, Dict

from src.composition.agent_services import AgentServices

_SERVICE_FIELDS = tuple(f.name for f in fields(AgentServices))
_DEFAULTS = {"drive_notes_enabled": True}


def user_agent_factory_kwargs(**kwargs: Any) -> Dict[str, Any]:
    services = {name: kwargs.pop(name, _DEFAULTS.get(name)) for name in _SERVICE_FIELDS}
    return {**kwargs, "services": AgentServices(**services)}
