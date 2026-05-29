"""Tests for core/logging.py — configure_prefect_bridge idempotency."""

from core.logging import configure_prefect_bridge, _bridge_configured


class TestConfigurePrefectBridge:
    def test_is_idempotent(self):
        """Calling configure_prefect_bridge multiple times should only add one sink."""
        import core.logging as logging_mod
        # Reset for test
        original = logging_mod._bridge_configured
        try:
            logging_mod._bridge_configured = False
            from loguru import logger
            initial_count = len(logger._core.handlers)
            
            configure_prefect_bridge()
            after_first = len(logger._core.handlers)
            
            configure_prefect_bridge()
            configure_prefect_bridge()
            after_multiple = len(logger._core.handlers)
            
            # Only one sink should be added, not one per call
            assert after_first == after_multiple
        finally:
            logging_mod._bridge_configured = original
