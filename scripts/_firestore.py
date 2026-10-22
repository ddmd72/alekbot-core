"""Firestore clients for scripts: always the named database from EnvironmentConfig, never `(default)`."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from google.cloud import firestore  # noqa: E402

from src.config.environment import EnvironmentConfig  # noqa: E402

# at import, so GOOGLE_CLOUD_PROJECT is set before _kwargs reads it
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _kwargs() -> dict:
    return {
        "project": os.getenv("GOOGLE_CLOUD_PROJECT") or None,
        "database": EnvironmentConfig().firestore_database_id,
    }


def async_client() -> firestore.AsyncClient:
    return firestore.AsyncClient(**_kwargs())


def sync_client() -> firestore.Client:
    return firestore.Client(**_kwargs())
