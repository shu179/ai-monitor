"""Selector diagnosis helpers.

The first slice of this package is intentionally small: shared DOM probing
helpers and stable config fingerprints. Runtime repair and UI flows can build
on these without changing the browser task control flow.
"""

from .dom_probe import click_interactive_candidate
from .fingerprint import browser_automation_hash, platform_browser_automation_hash
from .registry import FieldIntent, get_field_intent

__all__ = [
    "FieldIntent",
    "browser_automation_hash",
    "click_interactive_candidate",
    "get_field_intent",
    "platform_browser_automation_hash",
]
