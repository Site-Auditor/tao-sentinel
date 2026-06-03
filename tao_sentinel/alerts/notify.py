"""Notifier implementations for dispatching alerts.

A :class:`Notifier` knows how to deliver an :class:`~tao_sentinel.models.Alert`
somewhere: the console, a Telegram chat, or an arbitrary webhook. All
notifiers swallow delivery failures (logging a warning) so that a single broken
channel never crashes the watch loop.
"""

from __future__ import annotations

import abc
import logging

from tao_sentinel.models import Alert

logger = logging.getLogger(__name__)

# Map alert severity to a rich color/style name.
_SEVERITY_STYLES = {
    "info": "cyan",
    "warning": "yellow",
    "critical": "bold red",
}


class Notifier(abc.ABC):
    """Abstract base class for alert delivery channels."""

    @abc.abstractmethod
    def send(self, alert: Alert) -> None:
        """Deliver a single ``alert``.

        Implementations must not raise on delivery failure; they should log a
        warning instead so the watch loop keeps running.
        """
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    """Print alerts to the terminal using ``rich`` formatting.

    A console may be injected for testing; otherwise a default
    :class:`rich.console.Console` is created lazily.
    """

    def __init__(self, console=None) -> None:
        """Initialize the notifier, optionally with an injected rich console."""
        if console is None:
            from rich.console import Console

            console = Console()
        self._console = console

    def send(self, alert: Alert) -> None:
        """Render ``alert`` as a colored rich panel on the console."""
        try:
            from rich.panel import Panel

            style = _SEVERITY_STYLES.get(alert.severity, "white")
            netuid = f" [netuid {alert.netuid}]" if alert.netuid is not None else ""
            body = (
                f"{alert.message}\n\n"
                f"[dim]{alert.severity.upper()} - {alert.rule_type}"
                f"{netuid} - {alert.timestamp}[/dim]"
            )
            self._console.print(
                Panel(body, title=alert.title, border_style=style, expand=True)
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("ConsoleNotifier failed to render alert: %s", exc)


class TelegramNotifier(Notifier):
    """Deliver alerts to a Telegram chat via the Bot API ``sendMessage``."""

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0) -> None:
        """Store bot credentials and the target chat id."""
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout

    @property
    def _url(self) -> str:
        """Return the fully-qualified sendMessage endpoint."""
        return f"https://api.telegram.org/bot{self._bot_token}/sendMessage"

    def _format(self, alert: Alert) -> str:
        """Format an alert into a plain-text Telegram message body."""
        netuid = f" (netuid {alert.netuid})" if alert.netuid is not None else ""
        return (
            f"[{alert.severity.upper()}] {alert.title}{netuid}\n"
            f"{alert.message}\n"
            f"{alert.timestamp}"
        )

    def send(self, alert: Alert) -> None:
        """POST the alert to Telegram, logging (never raising) on failure."""
        import httpx

        payload = {"chat_id": self._chat_id, "text": self._format(alert)}
        try:
            resp = httpx.post(self._url, json=payload, timeout=self._timeout)
            if resp.status_code >= 400:
                logger.warning(
                    "TelegramNotifier got HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as exc:
            logger.warning("TelegramNotifier failed to send alert: %s", exc)


class WebhookNotifier(Notifier):
    """Deliver alerts by POSTing their JSON representation to a URL."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        """Store the webhook target URL."""
        self._url = url
        self._timeout = timeout

    def send(self, alert: Alert) -> None:
        """POST the alert as JSON, logging (never raising) on failure."""
        import httpx

        try:
            payload = alert.model_dump()
            resp = httpx.post(self._url, json=payload, timeout=self._timeout)
            if resp.status_code >= 400:
                logger.warning(
                    "WebhookNotifier got HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as exc:
            logger.warning("WebhookNotifier failed to send alert: %s", exc)


def build_notifiers(config) -> list[Notifier]:
    """Construct the list of notifiers enabled in ``config``.

    Always includes a :class:`ConsoleNotifier`. Adds a
    :class:`TelegramNotifier` when ``config.telegram`` is set and a
    :class:`WebhookNotifier` when ``config.webhook_url`` is set.
    """
    notifiers: list[Notifier] = [ConsoleNotifier()]
    telegram = getattr(config, "telegram", None)
    if telegram is not None:
        notifiers.append(
            TelegramNotifier(bot_token=telegram.bot_token, chat_id=telegram.chat_id)
        )
    webhook_url = getattr(config, "webhook_url", None)
    if webhook_url:
        notifiers.append(WebhookNotifier(url=webhook_url))
    return notifiers
