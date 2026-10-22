"""Post-sign-in return target: only a same-site relative path is ever followed."""
from typing import Optional

DEFAULT_RETURN_PATH = "/cabinet"


def safe_return_path(value: Optional[str]) -> str:
    """Return `value` if it is a same-site relative path, else `DEFAULT_RETURN_PATH`.

    Accepted: starts with a single `/` (so no scheme and no `//host`), carries no
    backslash (browsers read `/\\host` as `//host`) and no control character
    (a CR/LF/TAB inside a Location is stripped or splits it). Query strings are kept.
    """
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return DEFAULT_RETURN_PATH
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return DEFAULT_RETURN_PATH
    return value
