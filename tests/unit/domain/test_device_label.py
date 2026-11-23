"""`describe_device`: the label and platform hint of a new device session, from its User-Agent (CABINET_DEVICE_SESSIONS_RFC
§7.1). The User-Agent only selects words from a fixed vocabulary; nothing of it is copied."""
import pytest

from src.domain.device_label import describe_device

IPHONE_SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "Version/18.1 Mobile/15E148 Safari/604.1")
IPHONE_HOME_SCREEN = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                      "Mobile/15E148")
IPHONE_CHROME = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "CriOS/131.0 Mobile/15E148 Safari/604.1")
MAC_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
MAC_SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/18.1 Safari/605.1.15")
WINDOWS_EDGE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
                "Safari/537.36 Edg/131.0.0.0")
ANDROID_FIREFOX = "Mozilla/5.0 (Android 15; Mobile; rv:132.0) Gecko/132.0 Firefox/132.0"
IPAD_HOME_SCREEN = "Mozilla/5.0 (iPad; CPU OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
LINUX_CURL = "curl/8.7.1 (Linux)"


@pytest.mark.parametrize("user_agent, return_path, label, hint", [
    (IPHONE_HOME_SCREEN, "/cabinet/call", "iPhone — Home Screen (call)", "iPhone — Home Screen"),
    (IPHONE_HOME_SCREEN, "/cabinet/call/tutor?x=1", "iPhone — Home Screen (call)", "iPhone — Home Screen"),
    (IPHONE_HOME_SCREEN, "/cabinet", "iPhone — Home Screen", "iPhone — Home Screen"),
    (IPAD_HOME_SCREEN, "/cabinet", "iPad — Home Screen", "iPad — Home Screen"),
    (IPHONE_SAFARI, "/cabinet", "iPhone — Safari", "iPhone — Safari"),
    (IPHONE_CHROME, "/cabinet", "iPhone — Chrome", "iPhone — Chrome"),
    (MAC_CHROME, "/cabinet", "Mac — Chrome", "Mac — Chrome"),
    (MAC_SAFARI, "/cabinet/calls", "Mac — Safari", "Mac — Safari"),
    (WINDOWS_EDGE, "/cabinet", "Windows — Edge", "Windows — Edge"),
    (ANDROID_FIREFOX, "/cabinet", "Android — Firefox", "Android — Firefox"),
    (LINUX_CURL, "/cabinet", "Linux — Browser", "Linux — Browser"),
    ("", "/cabinet", "Unknown device — Browser", "Unknown device — Browser"),
])
def test_the_user_agent_selects_a_device_and_a_browser(user_agent, return_path, label, hint):
    assert describe_device(user_agent, return_path) == (label, hint)


def test_nothing_of_a_hostile_user_agent_is_copied():
    label, hint = describe_device("<!channel> *bold* {time} iPhone ignore previous instructions", "/cabinet")
    assert (label, hint) == ("iPhone — Home Screen", "iPhone — Home Screen")
