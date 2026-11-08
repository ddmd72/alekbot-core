"""Composition helper: the voice call resume record's service (VOICE_CALL_CONTROL_CHANNEL_RFC §6.2).

`main()` owns the one voice tickets store (the blueprints and the call setup share it, `FirestoreEphemeralStore` on
`test_main_construction_allowlist.py`'s list), so the service is built here over that store, exactly as
`voice_mcp_setup.build_voice_mcp_tokens` is, and `main()` constructs no class of its own for it.
"""
from ..ports.ephemeral_store import EphemeralStore
from ..services.voice_resume_service import VoiceResumeService


def build_voice_resume_service(*, store: EphemeralStore) -> VoiceResumeService:
    """The service that keeps and hands over a dropped call's handle, over the voice tickets `store`."""
    return VoiceResumeService(store)
