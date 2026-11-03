"""Supervisor under policy `abnormal`: it stops the server on the recycler's byte, like `always` (the server only
writes it after an abnormal request)."""
import os
import signal

from tests.unit.web.test_sandbox_supervisor import SERVER_PID, FakeKernel, make_supervisor, sigterms


def test_abnormal_stops_the_server_on_the_signal():
    kernel = FakeKernel()
    held = {}

    def spawn(write_fd):
        held["fd"] = os.dup(write_fd)
        os.write(held["fd"], b"1")
        return SERVER_PID

    code = make_supervisor(kernel, spawn, policy="abnormal").run()
    os.close(held["fd"])
    assert code == 0 and sigterms(kernel) == [(SERVER_PID, signal.SIGTERM)]
