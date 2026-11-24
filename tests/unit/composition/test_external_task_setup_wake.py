"""Wiring of the wake-up (EXTERNAL_TASK_QUEUE_RFC §5.8): the guard's own-host list comes from every deploy unit's
configured URL, and the service gets the cipher and the guard. Hosts are invented (`*.example.org`).
"""
from types import SimpleNamespace

from src.composition.external_task_setup import build_external_task_service, wake_own_hosts
from src.domain.wake_refusal import WakeRefusal
from src.domain.wake_target import WakeTarget
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue


def _auth() -> SimpleNamespace:
    return SimpleNamespace(oauth_redirect_uri="https://cabinet.example.org/auth/callback",
                           oauth_connect_redirect_uri="https://connect.example.org/auth/connect/callback",
                           mcp_resource_uri="https://mcp.example.org/mcp")


def test_the_own_hosts_are_every_configured_deploy_url_and_nothing_else():
    env = {"DRIVE_INDEXER_URL": "https://indexer.example.org", "SANDBOX_RENDER_URL": "https://render.example.org"}
    config = {"VOICE_RELAY_STREAM_URL": "wss://relay.example.org/stream", "SANDBOX_PARSE_URL": "https://parse.example.org",
              "OTHER_URL": "https://unrelated.example.org"}
    hosts = wake_own_hosts(config, _auth(), "https://svc.example.org", getenv=env.get)
    assert hosts == frozenset({"svc.example.org", "cabinet.example.org", "connect.example.org", "mcp.example.org",
                               "relay.example.org", "parse.example.org", "indexer.example.org", "render.example.org"})


def test_absent_optional_urls_are_skipped():
    hosts = wake_own_hosts({}, _auth(), "http://localhost:8080", getenv=lambda key: None)
    assert hosts == frozenset({"localhost", "cabinet.example.org", "connect.example.org", "mcp.example.org"})


async def test_the_service_refuses_an_own_host_at_save_time_and_is_none_without_a_queue():
    env = SimpleNamespace(external_tasks_collection="t", external_task_results_collection="r",
                          external_workers_collection="w", external_worker_keys_collection="k")
    queue, _ = make_task_queue()
    own = frozenset({"svc.example.org"})
    service = build_external_task_service(EvalDb(), env, task_queue=queue, user_repo=None, inactive_users=None,
                                          digest_key=b"k" * 32, cipher=AesGcmTestCipher(), own_hosts=own)
    assert await service._webhook.post(WakeTarget("https://svc.example.org/worker"), {}) is WakeRefusal.OWN_HOST
    assert build_external_task_service(EvalDb(), env, task_queue=None, user_repo=None, inactive_users=None,
                                       digest_key=b"k" * 32, cipher=AesGcmTestCipher(), own_hosts=own) is None
