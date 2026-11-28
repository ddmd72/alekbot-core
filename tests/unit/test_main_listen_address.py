"""The main service listens on all interfaces on Cloud Run and on loopback only for a local run."""
import main


def test_cloud_run_listens_on_all_interfaces():
    assert main.listen_address(True) == "0.0.0.0:8080"


def test_a_local_run_listens_on_loopback_only():
    assert main.listen_address(False) == "127.0.0.1:8080"
