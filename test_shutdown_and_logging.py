import importlib
import io
import logging
import unittest
from logging.handlers import RotatingFileHandler

import main
import core.shutdown as shutdown
from core.logging_utils import SecretRedactingFilter


class ShutdownAndLoggingTests(unittest.TestCase):
    def test_shutdown_callbacks_run_once_in_reverse_registration_order(self):
        module = importlib.reload(shutdown)
        calls: list[str] = []

        try:
            module.register_shutdown_callback("first", lambda: calls.append("first"))
            module.register_shutdown_callback("second", lambda: calls.append("second"))

            module.run_shutdown_callbacks("test")
            module.run_shutdown_callbacks("test-again")

            self.assertEqual(calls, ["second", "first"])
        finally:
            importlib.reload(module)

    def test_setup_logging_uses_rotating_file_handler(self):
        root = logging.getLogger()
        previous_handlers = list(root.handlers)
        for handler in previous_handlers:
            root.removeHandler(handler)

        try:
            main.setup_logging()
            rotating_handlers = [
                handler for handler in root.handlers
                if isinstance(handler, RotatingFileHandler)
            ]
            self.assertEqual(len(rotating_handlers), 1)
            self.assertEqual(rotating_handlers[0].maxBytes, 10 * 1024 * 1024)
            self.assertEqual(rotating_handlers[0].backupCount, 5)
        finally:
            for handler in list(root.handlers):
                handler.close()
                root.removeHandler(handler)
            for handler in previous_handlers:
                root.addHandler(handler)

    def test_setup_logging_adds_redaction_filter_to_existing_handlers(self):
        root = logging.getLogger()
        previous_handlers = list(root.handlers)
        previous_level = root.level
        for handler in previous_handlers:
            root.removeHandler(handler)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root.addHandler(handler)
        root.setLevel(logging.INFO)

        try:
            main.setup_logging()
            self.assertTrue(any(isinstance(item, SecretRedactingFilter) for item in handler.filters))

            root.info("api_key=dummy-test-secret webhook_url=https://example.com/hook")
            output = stream.getvalue()
            self.assertIn("api_key=***", output)
            self.assertIn("webhook_url=***", output)
            self.assertNotIn("dummy-test-secret", output)
        finally:
            handler.close()
            root.removeHandler(handler)
            root.setLevel(previous_level)
            for previous_handler in previous_handlers:
                root.addHandler(previous_handler)


if __name__ == "__main__":
    unittest.main()
