"""H3: the unit suite cannot leave the machine (tests/unit/conftest.py, tests/support/network_guard.py)."""

import asyncio
import os
import shutil
import socket
import sys
import tempfile
import urllib.request

import google.auth
import grpc
import grpc.aio
import pytest
from pytest_socket import SocketConnectBlockedError

from tests.support import network_guard
from tests.support.plugin_runs import isolate_switches, plugin_conftest

pytest_plugins = ["pytester"]

PUBLIC_IP = "203.0.113.7"  # TEST-NET-3: never routed, and the guard must refuse before any packet leaves


def _listener(family=socket.AF_INET, host="127.0.0.1"):
    server = socket.socket(family, socket.SOCK_STREAM)
    server.bind((host, 0))
    server.listen(1)
    return server


class TestSocketGuard:
    def test_connect_to_a_public_address_raises(self):
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(1)
        try:
            with pytest.raises(SocketConnectBlockedError, match=PUBLIC_IP):
                client.connect((PUBLIC_IP, 443))
        finally:
            client.close()

    def test_connect_to_a_public_ipv6_address_raises(self):
        try:
            client = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:
            pytest.skip("no IPv6 on this machine")
        try:
            with pytest.raises(SocketConnectBlockedError):
                client.connect(("2001:db8::1", 443, 0, 0))
        finally:
            client.close()

    def test_connect_ex_to_a_public_address_raises_too(self):
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(1)
        try:
            with pytest.raises(SocketConnectBlockedError, match=PUBLIC_IP):
                client.connect_ex((PUBLIC_IP, 443))
        finally:
            client.close()

    def test_connect_ex_to_loopback_works(self):
        server = _listener()
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2)
        try:
            assert client.connect_ex(server.getsockname()) == 0
        finally:
            client.close()
            server.close()

    def test_a_loopback_proxy_variable_cannot_tunnel_out(self, monkeypatch):
        """Cloud sessions set HTTPS_PROXY to a loopback port; honoured, it would carry requests past the socket guard."""
        import requests

        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        network_guard.install(monkeypatch)
        with pytest.raises(SocketConnectBlockedError, match=PUBLIC_IP):
            requests.get(f"http://{PUBLIC_IP}/", timeout=2)  # plain http: no TLS, no CA bundle involved

    def test_the_grpc_proxy_variable_is_removed_too(self, monkeypatch):
        """gRPC's C-core reads grpc_proxy before https_proxy, through sockets the socket guard never sees."""
        monkeypatch.setenv("grpc_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("GRPC_PROXY", "http://127.0.0.1:9")
        network_guard.install(monkeypatch)
        assert "grpc_proxy" not in os.environ and "GRPC_PROXY" not in os.environ

    def test_create_connection_to_a_public_address_raises(self):
        with pytest.raises(SocketConnectBlockedError):
            socket.create_connection((PUBLIC_IP, 443), timeout=1)

    def test_connect_to_loopback_works(self):
        server = _listener()
        try:
            with socket.create_connection(server.getsockname(), timeout=2) as client:
                conn, _ = server.accept()
                client.sendall(b"ping")
                assert conn.recv(4) == b"ping"
                conn.close()
        finally:
            server.close()

    def test_connect_to_ipv6_loopback_works(self):
        try:
            server = _listener(socket.AF_INET6, "::1")
        except OSError:
            pytest.skip("no IPv6 loopback on this machine")
        try:
            with socket.create_connection(server.getsockname()[:2], timeout=2):
                conn, _ = server.accept()
                conn.close()
        finally:
            server.close()

    def test_connect_to_a_unix_socket_works(self):
        # Not tmp_path: an AF_UNIX path is capped at 104 bytes on macOS (108 on Linux), and macOS's tmp_path
        # (/private/var/folders/…/pytest-of-<user>/pytest-<n>/<test name>/) is longer than that.
        directory = tempfile.mkdtemp(prefix="ng-", dir="/tmp")
        path = os.path.join(directory, "s.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
                conn, _ = server.accept()
                conn.close()
        finally:
            server.close()
            shutil.rmtree(directory, ignore_errors=True)

    def test_a_fresh_event_loop_works(self):
        loop = asyncio.new_event_loop()  # builds a socketpair: creation must stay allowed
        try:
            assert loop.run_until_complete(asyncio.sleep(0, "ok")) == "ok"
        finally:
            loop.close()

    async def test_asyncio_reaches_loopback_and_not_the_internet(self):
        async def handle(reader, writer):
            writer.write(await reader.read(4))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"echo")
            assert await reader.read(4) == b"echo"
            writer.close()
            with pytest.raises(SocketConnectBlockedError):
                await asyncio.open_connection(PUBLIC_IP, 443)
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.skipif(sys.platform != "darwin", reason="urllib consults the macOS system proxy only on darwin")
class TestSystemProxyCannotTunnelOut:
    """A local intercepting proxy set as the macOS system proxy (scutil --proxy) is read by urllib.request.getproxies()
    when no proxy variable is set, and requests, httpx, aiohttp and urllib all follow it to an allowed loopback port.
    A loopback listener stands in for that proxy: it must never be connected to."""

    @pytest.fixture
    def stand_in_proxy(self, monkeypatch):
        server = _listener()
        server.setblocking(False)
        url = "http://127.0.0.1:%d" % server.getsockname()[1]
        monkeypatch.setattr(urllib.request, "getproxies_macosx_sysconf", lambda: {"http": url, "https": url})
        network_guard.install(monkeypatch)
        yield server
        try:
            with pytest.raises(BlockingIOError):
                server.accept()  # nothing ever connected to the stand-in proxy
        finally:
            server.close()

    def test_requests_does_not_follow_the_system_proxy(self, stand_in_proxy):
        import requests

        with pytest.raises(SocketConnectBlockedError, match=PUBLIC_IP):
            requests.get(f"http://{PUBLIC_IP}/", timeout=2)

    def test_httpx_does_not_follow_the_system_proxy(self, stand_in_proxy):
        import httpx

        with pytest.raises(SocketConnectBlockedError, match=PUBLIC_IP):
            httpx.get(f"http://{PUBLIC_IP}/", timeout=2)

    def test_urllib_does_not_follow_the_system_proxy(self, stand_in_proxy):
        with pytest.raises(SocketConnectBlockedError, match=PUBLIC_IP):  # a RuntimeError: urllib does not wrap it in URLError
            urllib.request.urlopen(f"http://{PUBLIC_IP}/", timeout=2)

    def test_a_test_that_rewrites_the_environment_still_gets_no_proxy(self, stand_in_proxy, monkeypatch):
        """prod_env.seal() empties os.environ after the guard: a NO_PROXY set by the caller does not survive it."""
        import httpx

        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert urllib.request.getproxies() == {}
        with pytest.raises(SocketConnectBlockedError, match=PUBLIC_IP):
            httpx.get(f"http://{PUBLIC_IP}/", timeout=2)


class TestGrpcTripwire:
    def test_google_auth_default_raises_with_the_rule(self):
        with pytest.raises(network_guard.NetworkGuardError, match="google.auth.default"):
            google.auth.default()

    def test_secure_channel_to_a_google_host_raises_and_says_how_to_fix_it(self):
        with pytest.raises(network_guard.NetworkGuardError) as raised:
            grpc.secure_channel("firestore.googleapis.com:443", grpc.ssl_channel_credentials())
        assert "firestore.googleapis.com:443" in str(raised.value)
        assert "tests/integration" in str(raised.value) and "fake" in str(raised.value)

    def test_secure_channel_to_localhost_passes(self):
        for target in ("localhost:50051", "127.0.0.1:50051", "[::1]:50051", "dns:///localhost:50051"):
            grpc.secure_channel(target, grpc.ssl_channel_credentials()).close()

    async def test_aio_secure_channel_to_a_google_host_raises(self):
        with pytest.raises(network_guard.NetworkGuardError, match="grpc.aio.secure_channel"):
            grpc.aio.secure_channel("firestore.googleapis.com:443", grpc.ssl_channel_credentials())

    async def test_aio_secure_channel_to_localhost_passes(self):
        channel = grpc.aio.secure_channel("localhost:50051", grpc.ssl_channel_credentials())
        await channel.close()

    def test_a_look_alike_host_is_not_loopback(self):
        with pytest.raises(network_guard.NetworkGuardError):
            grpc.secure_channel("localhost.evil.example:443", grpc.ssl_channel_credentials())

    @pytest.mark.parametrize(
        "target, host",
        [
            ("firestore.googleapis.com:443", "firestore.googleapis.com"),
            ("dns:///localhost:8080", "localhost"),
            ("dns://8.8.8.8/example.com:443", "example.com"),
            ("ipv4:127.0.0.1:80", "127.0.0.1"),
            ("[::1]:50051", "::1"),
            ("::1", "::1"),
            ("localhost", "localhost"),
            (b"localhost:1", "localhost"),
        ],
    )
    def test_target_host(self, target, host):
        assert network_guard.target_host(target) == host

    def test_unix_targets_are_local(self):
        assert network_guard.is_loopback_target("unix:/tmp/x.sock")
        assert not network_guard.is_loopback_target("10.0.0.1:80")


class TestGuardIsPerTest:
    def test_install_is_undone_after_a_test(self, pytester, monkeypatch):
        """The patches go through monkeypatch and pytest-socket's teardown: nothing leaks into the next test."""
        isolate_switches(monkeypatch)
        original = grpc.secure_channel
        pytester.makeconftest(plugin_conftest("network_guard"))
        pytester.makepyfile(
            test_inner="""
import socket
import pytest

def test_blocked():
    with pytest.raises(RuntimeError, match="203.0.113.7"):
        socket.create_connection(("203.0.113.7", 443), timeout=1)

def test_public_connect_fails_the_test():
    socket.create_connection(("203.0.113.7", 443), timeout=1)
"""
        )
        result = pytester.runpytest("-p", "no:cacheprovider", "-rA")
        result.assert_outcomes(passed=1, failed=1)
        result.stdout.fnmatch_lines(["PASSED test_inner.py::test_blocked", "FAILED test_inner.py::test_public_connect_fails_the_test*"])
        assert grpc.secure_channel is original
