import inspect
from src.ports.telephony_port import TelephonyPort


def test_telephony_port_is_abstract():
    assert inspect.isabstract(TelephonyPort)


def test_telephony_port_declares_originate_call():
    assert "originate_call" in dir(TelephonyPort)
