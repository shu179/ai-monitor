from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.article_payload_migration_service import compute_throttle_delay  # noqa: E402


class ComputeThrottleDelayTests(unittest.TestCase):
    def test_no_delay_at_or_under_budget(self) -> None:
        self.assertEqual(compute_throttle_delay(0, max_active=20), 0.0)
        self.assertEqual(compute_throttle_delay(20, max_active=20), 0.0)

    def test_delay_grows_linearly_over_budget(self) -> None:
        self.assertEqual(compute_throttle_delay(25, max_active=20, seconds_per_backend=1.0), 5.0)
        self.assertEqual(compute_throttle_delay(23, max_active=20, seconds_per_backend=2.0), 6.0)

    def test_delay_is_capped(self) -> None:
        self.assertEqual(
            compute_throttle_delay(10_000, max_active=20, max_backoff_seconds=30.0),
            30.0,
        )

    def test_negative_or_zero_max_active_is_clamped(self) -> None:
        # max_active is clamped to at least 1 so a misconfiguration cannot divide-by-zero
        # or make every batch sleep forever.
        self.assertEqual(compute_throttle_delay(1, max_active=0), 0.0)
        self.assertGreater(compute_throttle_delay(5, max_active=0), 0.0)


if __name__ == "__main__":
    unittest.main()
