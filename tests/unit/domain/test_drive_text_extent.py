from src.domain.drive_text_extent import ExtentState, ExtentWhy, TextExtent


def test_enum_values_are_the_log_strings():
    assert [s.value for s in ExtentState] == ["pageable", "stale", "not_indexed"]
    assert [w.value for w in ExtentWhy] == ["off", "not_connected", "rebuilding", "not_read", "no_text", "hidden",
                                            "unknown"]


def test_unknown_is_not_indexed():
    assert TextExtent.UNKNOWN.state is ExtentState.NOT_INDEXED
    assert TextExtent.UNKNOWN.why is ExtentWhy.UNKNOWN
