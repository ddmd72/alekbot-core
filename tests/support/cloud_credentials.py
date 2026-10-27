"""Credential selection of the cloud gate: pure, so the branches are tested offline."""
from typing import Any, Callable


def select_credentials(source: Any, sa: str, impersonate: Callable[[Any], Any]) -> Any:
    """Credentials of the test service account `sa`, or a refusal (`PermissionError`).

    Default credentials that already identify as `sa` (Workload Identity Federation in CI) are used as they are;
    anything else is impersonated with `impersonate(source)`. Both paths end in the same email check."""
    if getattr(source, "service_account_email", None) == sa:
        credentials = source
    else:
        credentials = impersonate(source)
    if getattr(credentials, "service_account_email", None) != sa:
        raise PermissionError("the credentials are not those of the test service account")
    return credentials
