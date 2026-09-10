"""Shared fixtures for tuya-local tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_discovery_sockets():
    """Stop async_setup from opening UDP sockets during tests.

    Only the component level hook is stubbed, so tests can still drive
    TuyaLocalDiscovery directly. This deliberately avoids depending on the
    ``mocker`` fixture, which would change fixture teardown ordering for
    every test in the suite.
    """
    with patch("custom_components.tuya_local.async_start_discovery"):
        yield
