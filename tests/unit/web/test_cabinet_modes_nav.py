"""Call and Modes left the Cabinet menu (CABINET_REDESIGN_RFC §4): the call button and the modes link on Home carry
translated labels. Where they point and that the menu no longer holds them: `test_cabinet_shell.py`
(`test_home_offers_the_call_then_the_modes_link_then_a_tile_per_section`, `test_call_and_modes_leave_the_menu`)."""
import re

from tests.unit.web.cabinet_source import cabinet_source

_HTML = cabinet_source()


def test_home_call_and_modes_labels_are_bound_to_their_translated_keys():
    for element, key in (("home-call-title", "home.call_title"), ("home-call-sub", "home.call_sub"),
                         ("home-modes-label", "home.modes")):
        assert f'id="{element}"' in _HTML, element
        assert re.search(r'"#' + re.escape(element) + r'":\s+"' + re.escape(key) + '"', _HTML), element
        assert len(re.findall(r'"' + re.escape(key) + r'": "[^"]+"', _HTML)) == 4, key
