"""Handles for drive adapter tests (MULTI_DRIVE_RFC §3, §6): technical ids only."""
from src.domain.drive_handle import DriveHandle

ONEDRIVE_KIND = "microsoft_onedrive"


def drive_handle(user_id: str = "usr-1", connection_id: str = "conn-1", credential_id: str = "cred-1",
                 kind: str = ONEDRIVE_KIND) -> DriveHandle:
    """A handle of one connection; two calls with different ids give two connections of one adapter."""
    return DriveHandle(user_id=user_id, connection_id=connection_id, credential_id=credential_id, kind=kind)
