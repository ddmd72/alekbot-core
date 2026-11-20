"""MapsToolError (domain exception for tool execution failures), defined in the MapsToolsPort module."""

import pytest

from src.ports.maps_tools_port import MapsToolError


class TestMapsToolError:
    """MapsToolError is a domain exception (not adapter-internal)."""

    def test_is_exception_subclass(self):
        assert issubclass(MapsToolError, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(MapsToolError, match="quota exceeded"):
            raise MapsToolError("quota exceeded")

    def test_str_representation(self):
        err = MapsToolError("location unavailable")
        assert "location unavailable" in str(err)
