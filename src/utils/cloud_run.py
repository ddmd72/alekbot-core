"""Cloud Run detection, in one place: a service sets K_SERVICE, a job sets CLOUD_RUN_JOB (K_JOB is not a thing)."""
from __future__ import annotations

import os
from typing import Callable, Optional


def on_cloud_run(getenv: Callable[[str], Optional[str]] = os.environ.get) -> bool:
    return bool(getenv("K_SERVICE") or getenv("CLOUD_RUN_JOB"))
