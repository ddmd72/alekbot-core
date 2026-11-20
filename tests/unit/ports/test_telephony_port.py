"""Keeps telephony_port imported for the untested-modules guard."""
import inspect
from src.ports.telephony_port import TelephonyPort


def test_telephony_port_is_abstract():
    assert inspect.isabstract(TelephonyPort)
