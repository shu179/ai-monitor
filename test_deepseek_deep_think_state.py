import unittest

from platforms.deepseek import DeepSeekPlatform


class _DeepThinkProbe(DeepSeekPlatform):
    def __init__(self, *, target_enabled: bool, states: list[dict], click_success: bool = True) -> None:
        self.name = "DeepSeek"
        self.deep_think = target_enabled
        self._states = list(states)
        self._click_success = click_success
        self._deep_think_cached_state = None
        self.clicks = 0

    def _raise_if_stop_requested(self) -> None:
        return None

    def _reraise_stop_requested(self, exc: Exception) -> None:
        return None

    def _cooperative_sleep(self, seconds: float) -> None:
        return None

    def _get_deep_think_state(self):
        if self._states:
            return self._states.pop(0)
        return {"found": True, "active": None, "confidence": "low"}

    def _click_deep_think_toggle(self) -> bool:
        self.clicks += 1
        return self._click_success

    def _wait_for_deep_think_state(self, expected_active: bool, timeout_seconds: float = 1.5):
        del timeout_seconds
        if self._states:
            return self._states.pop(0)
        return {"found": True, "active": expected_active, "confidence": "high"}


class DeepSeekDeepThinkStateTests(unittest.TestCase):
    def test_enable_keeps_already_active_without_clicking(self) -> None:
        platform = _DeepThinkProbe(
            target_enabled=True,
            states=[{"found": True, "active": True, "confidence": "high"}],
        )

        self.assertTrue(platform.enable_deep_think())
        self.assertEqual(platform.clicks, 0)
        self.assertTrue(platform._deep_think_cached_state)

    def test_enable_clicks_when_inactive_and_confirms_active(self) -> None:
        platform = _DeepThinkProbe(
            target_enabled=True,
            states=[
                {"found": True, "active": False, "confidence": "high"},
                {"found": True, "active": True, "confidence": "high"},
            ],
        )

        self.assertTrue(platform.enable_deep_think())
        self.assertEqual(platform.clicks, 1)
        self.assertTrue(platform._deep_think_cached_state)

    def test_disable_clicks_when_active_and_confirms_inactive(self) -> None:
        platform = _DeepThinkProbe(
            target_enabled=False,
            states=[
                {"found": True, "active": True, "confidence": "high"},
                {"found": True, "active": False, "confidence": "high"},
            ],
        )

        self.assertFalse(platform.enable_deep_think())
        self.assertEqual(platform.clicks, 1)
        self.assertFalse(platform._deep_think_cached_state)

    def test_enable_uses_cached_active_state_when_dom_state_is_ambiguous(self) -> None:
        platform = _DeepThinkProbe(
            target_enabled=True,
            states=[{"found": True, "active": None, "confidence": "low"}],
        )
        platform._deep_think_cached_state = True

        self.assertTrue(platform.enable_deep_think())
        self.assertEqual(platform.clicks, 0)
        self.assertTrue(platform._deep_think_cached_state)

    def test_enable_sets_cache_when_click_succeeds_but_state_stays_ambiguous(self) -> None:
        platform = _DeepThinkProbe(
            target_enabled=True,
            states=[
                {"found": True, "active": False, "confidence": "high"},
                {"found": True, "active": None, "confidence": "low"},
            ],
        )

        self.assertTrue(platform.enable_deep_think())
        self.assertEqual(platform.clicks, 1)
        self.assertTrue(platform._deep_think_cached_state)


if __name__ == "__main__":
    unittest.main()
