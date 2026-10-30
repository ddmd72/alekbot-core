"""A breadth-first walk over a built object graph (P9 design §5a, §5e).

It reads the values production builds, so it needs no map from constructor parameters to attributes. Two products:

- `none_attributes(walk(root))`: every attribute of a reportable object that is `None`, keyed by the shortest path from
  the root. The walk enters everything, agents included.
- `instance_counts(walk_outside_agents(root))`: how many instances of each adapter, service, handler and composition
  class the container-owned part holds. That walk does not enter a `BaseAgent`, so the per-agent copies of a proxy or a
  prompt builder are not counted. `store_counts(walk(root))` counts the persistent-store adapters over the full walk.

What is walked: instances of classes defined under `src.`, plus the containers that hold them (list, tuple, set,
dict, and the read-only `types.MappingProxyType` view of a dict), bound methods (through `__self__`), closures (through the cells) and `functools.partial` (function, arguments).
Third-party objects (the Slack app, the Quart app, SDK clients) are leaves. Pydantic models and classes under
`src/domain/` are traversed but never reported: their `None` fields are data, not wiring.
"""
import functools
import types
from collections import Counter, deque
from typing import Any, Callable, Deque, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

from pydantic import BaseModel

SRC = "src."
DOMAIN = "src.domain."
COUNTED_LAYERS = ("src.adapters.", "src.services.", "src.handlers.", "src.composition.")
COUNTED_CLASSES = ("src.infrastructure.agent_coordinator.AgentCoordinator", "src.infrastructure.agent_registry.AgentRegistry")
# Persistent-store adapters are also counted over the full walk, agents included (a second repository built inside
# an agent build never reaches the container-owned count).
STORE_MODULE_PREFIXES = ("firestore_", "gcs_", "bigquery_")


class Node(NamedTuple):
    path: str
    obj: Any


def is_src_instance(obj: Any) -> bool:
    cls = type(obj)
    return cls.__module__.startswith(SRC) and not isinstance(obj, (type, types.ModuleType, types.FunctionType))


def is_reportable(obj: Any) -> bool:
    """False for pydantic models and for classes under src/domain/ (R3-S2)."""
    cls = type(obj)
    return not (isinstance(obj, BaseModel) or cls.__module__.startswith(DOMAIN))


def is_agent(obj: Any) -> bool:
    from src.agents.base_agent import BaseAgent
    return isinstance(obj, BaseAgent)


def _slot_names(cls: type) -> List[str]:
    names: List[str] = []
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        names.extend([slots] if isinstance(slots, str) else slots)
    return sorted(n for n in names if n not in ("__dict__", "__weakref__"))


def attributes(obj: Any) -> Dict[str, Any]:
    """The instance attributes of a source object, `__dict__` and `__slots__`."""
    found = dict(vars(obj)) if hasattr(obj, "__dict__") else {}
    for name in _slot_names(type(obj)):
        if hasattr(obj, name):
            found.setdefault(name, getattr(obj, name))
    return found


def _set_order(items: Set[Any]) -> List[Any]:
    return sorted(items, key=lambda i: (type(i).__qualname__, repr(i) if isinstance(i, (str, int, float, bool)) else ""))


def children(path: str, obj: Any) -> Iterator[Tuple[str, Any]]:
    """The edges out of `obj`: (child path, child). Sorted so the shortest path is stable."""
    if isinstance(obj, functools.partial):
        yield f"{path}.func", obj.func
        for i, arg in enumerate(obj.args):
            yield f"{path}.args[{i}]", arg
        for key in sorted(obj.keywords):
            yield f"{path}.keywords[{key}]", obj.keywords[key]
    elif isinstance(obj, types.MethodType):
        yield f"{path}.__self__", obj.__self__
    elif isinstance(obj, types.FunctionType):
        for name, cell in zip(obj.__code__.co_freevars, obj.__closure__ or ()):
            try:
                value = cell.cell_contents
            except ValueError:  # an empty cell
                continue
            yield f"{path}.<closure {name}>", value
    elif isinstance(obj, (dict, types.MappingProxyType)):
        for key in sorted(obj, key=lambda k: (type(k).__qualname__, repr(k))):
            label = repr(key) if isinstance(key, (str, int, float, bool, tuple)) else f"<{type(key).__name__}>"
            yield f"{path}[{label}]", obj[key]
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            yield f"{path}[{i}]", item
    elif isinstance(obj, (set, frozenset)):
        for i, item in enumerate(_set_order(obj)):
            yield f"{path}{{{i}}}", item
    elif is_src_instance(obj):
        for name, value in sorted(attributes(obj).items()):
            yield f"{path}.{name}", value


def _walkable(obj: Any) -> bool:
    return isinstance(obj, (functools.partial, types.MethodType, types.FunctionType, dict, types.MappingProxyType,
                            list, tuple, set, frozenset)) \
        or is_src_instance(obj)


def walk(root: Any, name: str = "app", enter: Callable[[Any], bool] = lambda obj: True) -> List[Node]:
    """Every walkable object reachable from `root`, breadth first, with its shortest path.

    `enter(obj)` false: the object is listed but its edges are not followed.
    """
    seen: Set[int] = {id(root)}
    order: List[Node] = [Node(name, root)]
    queue: Deque[Node] = deque(order)
    while queue:
        node = queue.popleft()
        if not enter(node.obj):
            continue
        for path, child in children(node.path, node.obj):
            if id(child) in seen or not _walkable(child):
                continue
            seen.add(id(child))
            found = Node(path, child)
            order.append(found)
            queue.append(found)
    return order


class NoneAttribute(NamedTuple):
    path: str
    owner: type
    attr: str


def reportable(nodes: List[Node]) -> List[Node]:
    return [n for n in nodes if is_src_instance(n.obj) and is_reportable(n.obj)]


def none_attributes(nodes: List[Node]) -> List[NoneAttribute]:
    """Every `None` attribute of a reportable source object, with the shortest path to it."""
    found = []
    for node in reportable(nodes):
        for attr, value in sorted(attributes(node.obj).items()):
            if value is None:
                found.append(NoneAttribute(f"{node.path}.{attr}", type(node.obj), attr))
    return found


def attribute_values(nodes: List[Node]) -> Dict[Tuple[str, str], List[Any]]:
    """`{(class name, attr): the value on every reportable instance of that class or a subclass that has the attribute}`."""
    index: Dict[Tuple[str, str], List[Any]] = {}
    for node in reportable(nodes):
        for attr, value in attributes(node.obj).items():
            for klass in type(node.obj).__mro__:
                index.setdefault((klass.__name__, attr), []).append(value)
    return index


def _qualified(obj: Any) -> str:
    return f"{type(obj).__module__}.{type(obj).__qualname__}"


def instance_counts(nodes: List[Node]) -> Counter:
    """Instances per counted class among `nodes` (a walk that did not enter any `BaseAgent`).

    Counted: adapters, services, handlers and composition classes, plus `AgentCoordinator` and `AgentRegistry`.
    """
    counts: Counter = Counter()
    for node in nodes:
        name = _qualified(node.obj)
        counted = type(node.obj).__module__.startswith(COUNTED_LAYERS) or name in COUNTED_CLASSES
        if is_src_instance(node.obj) and counted and not is_agent(node.obj):
            counts[name] += 1
    return counts


def store_counts(nodes: List[Node]) -> Counter:
    """Instances of the persistent-store adapter classes (`firestore_*`, `gcs_*`, `bigquery_*` modules) among `nodes`."""
    counts: Counter = Counter()
    for node in nodes:
        module = type(node.obj).__module__
        if is_src_instance(node.obj) and module.startswith("src.adapters.") \
                and module.rsplit(".", 1)[-1].startswith(STORE_MODULE_PREFIXES):
            counts[_qualified(node.obj)] += 1
    return counts


def walk_outside_agents(root: Any, name: str = "app") -> List[Node]:
    """The container-owned part: the walk does not enter a `BaseAgent`, so per-agent copies are not reached."""
    return walk(root, name, enter=lambda obj: not is_agent(obj))
