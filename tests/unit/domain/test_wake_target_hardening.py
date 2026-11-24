"""The guard's pure rules, hardening round (EXTERNAL_TASK_QUEUE_RFC §5.8): IPv6 site-local addresses and a user
`Accept-Encoding` header are refused."""
import pytest

from src.domain.wake_refusal import WakeRefusal
from src.domain.wake_target import address_is_public, check_headers, parse_header_lines


@pytest.mark.parametrize("address", ["fec0::1", "fec0:0:0:1::5", "feff:ffff::1"])
def test_ipv6_site_local_addresses_are_refused(address):
    assert address_is_public(address) is False


@pytest.mark.parametrize("name", ["Accept-Encoding", "accept-encoding", "ACCEPT-ENCODING"])
def test_a_user_accept_encoding_header_is_refused(name):
    assert check_headers(((name, "gzip"),)) is WakeRefusal.HEADER
    assert parse_header_lines(f"{name}: br") is WakeRefusal.HEADER
