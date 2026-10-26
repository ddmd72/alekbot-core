"""How an environment switch is read: a value is an explicit off, or it leaves the switch on its default."""
from typing import Final, Optional

_OFF_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


def is_off_value(raw: Optional[str]) -> bool:
    """True for an explicit off value (`0`, `false`, `no`, `off`, any case, surrounding spaces ignored); unset, empty
    or any other text is not one."""
    return (raw or "").strip().lower() in _OFF_VALUES
