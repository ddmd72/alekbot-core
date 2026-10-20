"""Recycle supervisor prototype for the S0 spike (the real one is S1 T5; this is what runbook item b measures).

Starts Hypercorn as a child and exports its own pid as SPIKE_SUPERVISOR_PID. When the app sends SIGUSR1 (after it has
streamed a /recycle reply) the supervisor sends SIGTERM to the child, which finishes the in-flight response in its
graceful shutdown; then the supervisor exits 0 and Cloud Run replaces the instance. SIGTERM from the platform is
forwarded to the child. Output goes through `print(flush=True)` (standalone spike script; not a src/ module).
"""
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GRACEFUL_S = os.getenv("SPIKE_GRACEFUL_S", "30")


def main() -> int:
    port = os.getenv("PORT", "8080")
    env = dict(os.environ, SPIKE_SUPERVISOR_PID=str(os.getpid()))
    child = subprocess.Popen(
        [sys.executable, "-m", "hypercorn", "app:app", "--bind", f"0.0.0.0:{port}", "--workers", "1",
         "--graceful-timeout", GRACEFUL_S, "--keep-alive", "75"],
        cwd=HERE, env=env)
    state = {"recycle_at": 0.0}

    def on_usr1(_signum: int, _frame: object) -> None:
        if not state["recycle_at"]:
            state["recycle_at"] = time.monotonic()
            print("[supervisor] recycle requested: SIGTERM to the server", flush=True)
            child.send_signal(signal.SIGTERM)

    def on_term(_signum: int, _frame: object) -> None:
        print("[supervisor] platform SIGTERM: forwarding", flush=True)
        child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGUSR1, on_usr1)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    code = child.wait()
    if state["recycle_at"]:
        print(f"[supervisor] server drained {time.monotonic() - state['recycle_at']:.2f}s after the request "
              f"(exit {code}); exiting 0", flush=True)
        return 0
    print(f"[supervisor] server exited on its own with {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
