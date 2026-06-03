"""Alerting subsystem for tao-sentinel.

Re-exports the watch engine, the rule registry, and the notifier classes so
callers can simply ``from tao_sentinel.alerts import WatchEngine, RULES,
ConsoleNotifier``.
"""

from __future__ import annotations

from tao_sentinel.alerts.engine import WatchEngine
from tao_sentinel.alerts.notify import (
    ConsoleNotifier,
    Notifier,
    TelegramNotifier,
    WebhookNotifier,
    build_notifiers,
)
from tao_sentinel.alerts.rules import (
    RULES,
    emission_shift_rule,
    price_change_rule,
    stake_change_rule,
    validator_dereg_rule,
)

__all__ = [
    "WatchEngine",
    "RULES",
    "Notifier",
    "ConsoleNotifier",
    "TelegramNotifier",
    "WebhookNotifier",
    "build_notifiers",
    "price_change_rule",
    "stake_change_rule",
    "validator_dereg_rule",
    "emission_shift_rule",
]
