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

# Severity ordering for digest grouping (most severe first) and escalation.
_SEVERITY_ORDER = ("critical", "warning", "info")

# Telegram messages cap at 4096 chars; stay well under to leave headroom for
# the API envelope and any trailing truncation marker.
_TELEGRAM_MAX_CHARS = 3500


class Notifier(abc.ABC):
    """Abstract base class for alert delivery channels."""

    @abc.abstractmethod
    def send(self, alert: Alert) -> None:
        """Deliver a single ``alert``.

        Implementations must not raise on delivery failure; they should log a
        warning instead so the watch loop keeps running.
        """
        raise NotImplementedError

    def send_many(self, alerts: list[Alert]) -> None:
        """Deliver a batch of ``alerts``.

        The default loops over :meth:`send` (one delivery per alert). Channels
        that benefit from batching (e.g. a single combined chat message) should
        override this. Like :meth:`send`, must not raise on delivery failure.
        """
        for alert in alerts:
            self.send(alert)


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

    def _format_digest(self, alerts: list[Alert]) -> str:
        """Format a batch of alerts into one severity-grouped digest body.

        Alerts are grouped under ``CRITICAL`` / ``WARNING`` / ``INFO`` headers
        (most severe first; unknown severities fall under a trailing ``OTHER``
        group). The body is capped at :data:`_TELEGRAM_MAX_CHARS`; once adding
        the next line would exceed the cap it is truncated with a
        ``...and N more`` marker so a flood of alerts can never produce a
        message Telegram will reject.
        """
        # Group by severity, preserving input order within each group.
        groups: dict[str, list[Alert]] = {}
        for alert in alerts:
            groups.setdefault(alert.severity, []).append(alert)

        ordered_severities = [s for s in _SEVERITY_ORDER if s in groups]
        ordered_severities += [s for s in groups if s not in _SEVERITY_ORDER]

        lines: list[str] = []
        for severity in ordered_severities:
            label = severity.upper() if severity in _SEVERITY_ORDER else "OTHER"
            group = groups[severity]
            lines.append(f"== {label} ({len(group)}) ==")
            for alert in group:
                netuid = (
                    f" (netuid {alert.netuid})" if alert.netuid is not None else ""
                )
                lines.append(f"- {alert.title}{netuid}")

        body = ""
        total = len(alerts)
        rendered = 0
        for line in lines:
            candidate = line if not body else body + "\n" + line
            # Reserve room for a possible truncation marker.
            marker = f"\n...and {total - rendered} more"
            if len(candidate) + len(marker) > _TELEGRAM_MAX_CHARS:
                remaining = total - rendered
                body += f"\n...and {remaining} more"
                return body
            body = candidate
            # Only header lines start with "==": don't count them as alerts.
            if not line.startswith("== "):
                rendered += 1
        return body

    def send(self, alert: Alert) -> None:
        """POST the alert to Telegram, logging (never raising) on failure."""
        self._post(self._format(alert))

    def send_many(self, alerts: list[Alert]) -> None:
        """Send all ``alerts`` as ONE combined, severity-grouped digest message.

        Collapsing a batch into a single message avoids hammering the Telegram
        rate limit (and the user's notifications) when many watches fire on the
        same tick. An empty batch sends nothing.
        """
        if not alerts:
            return
        self._post(self._format_digest(alerts))

    def _post(self, text: str) -> None:
        """POST ``text`` to the Telegram chat, logging (never raising) on failure."""
        import httpx

        payload = {"chat_id": self._chat_id, "text": text}
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
