"""Command-line interface for tao-sentinel.

A :mod:`typer` application exposing the project's functionality:

* ``init``      - write an example configuration file.
* ``watch``     - run the alert engine (once, or as a polling loop).
* ``portfolio`` - value a coldkey's stake positions.
* ``scan``      - score subnet health and print a graded table.
* ``serve``     - run the read-only web dashboard via uvicorn.

Every command accepts ``--mock`` so the whole tool works without a Taostats
API key by using the deterministic :class:`~tao_sentinel.api.MockTaostatsClient`.
This module is the only place (besides the notifiers) that talks directly to
the terminal; library code uses :mod:`logging`.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import pydantic
import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .api import TaostatsError, make_client
from .config import load_config, write_example_config
from .portfolio import PortfolioTracker
from .scanner import SubnetScanner

logger = logging.getLogger(__name__)

#: Exceptions that represent a user/config/runtime error (bad config file,
#: invalid YAML, a config that fails validation, or an API failure). These are
#: surfaced as a short message to stderr plus a non-zero exit, never as a raw
#: traceback.
#:
#: ``ValueError`` covers a config file that parses as valid YAML but is not a
#: mapping (``load_config`` raises a plain ``ValueError`` for a top-level scalar
#: or list). Note that ``pydantic.ValidationError`` is itself a subclass of
#: ``ValueError`` but is listed explicitly for documentation; a *plain*
#: ``ValueError`` is not a ``pydantic.ValidationError`` and so would otherwise
#: escape uncaught. These error types are only ever expected from a
#: ``load_config`` (or client) call, so catching ``ValueError`` here cannot mask
#: an unrelated programming bug elsewhere.
_USER_ERRORS = (
    FileNotFoundError,
    yaml.YAMLError,
    ValueError,
    pydantic.ValidationError,
    TaostatsError,
)

#: Default configuration file path used by commands that read/write config.
DEFAULT_CONFIG_PATH = "sentinel.yaml"

app = typer.Typer(
    name="tao-sentinel",
    help="A Bittensor watchtower built on the Taostats API.",
    add_completion=False,
    no_args_is_help=True,
)

#: Shared rich console for all human-facing output.
console = Console()

#: Console bound to stderr for error messages, so error text never pollutes the
#: stdout data stream (e.g. the ``--json`` output of ``scan``/``portfolio``).
error_console = Console(stderr=True)

# Letter-grade -> rich style for colorized scan output.
_GRADE_STYLES = {
    "A": "bold green",
    "B": "green",
    "C": "yellow",
    "D": "orange1",
    "F": "bold red",
}


def _configure_logging(verbose: bool) -> None:
    """Configure stdlib logging once for the CLI process.

    Args:
        verbose: When ``True`` use ``DEBUG`` level, otherwise ``WARNING`` so
            normal command output is not cluttered by library logs.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _resolved_api_key(config_obj: Optional[object]) -> Optional[str]:
    """Return the resolved API key from a config object, if any.

    Args:
        config_obj: A loaded :class:`~tao_sentinel.config.Config`, or ``None``.

    Returns:
        The configured (already env-resolved) API key, or ``None``.
    """
    if config_obj is None:
        return None
    return getattr(config_obj, "api_key", None)


def _round_floats(value: object, ndigits: int = 9) -> object:
    """Recursively round floats in a JSON-ready structure.

    Float summation leaves artifacts like ``87.55000000000001`` in totals;
    round to 9 decimals (full RAO precision, 1e-9 TAO) so the JSON stream
    carries the same numbers a human would read, with no information loss.
    """
    if isinstance(value, float):
        return round(value, ndigits)
    if isinstance(value, dict):
        return {k: _round_floats(v, ndigits) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_floats(v, ndigits) for v in value]
    return value


def _print_json(payload: object) -> None:
    """Print ``payload`` as indented JSON to stdout."""
    console.print_json(json.dumps(_round_floats(payload), default=str))


def _fail(message: str) -> typer.Exit:
    """Print ``message`` in red to stderr and return a :class:`typer.Exit(1)`.

    Callers ``raise _fail(...)`` so a user-facing error (bad config file,
    invalid YAML, a config that fails validation, or an API failure) becomes a
    concise stderr message plus a non-zero exit instead of a raw traceback. The
    message goes to stderr so it never contaminates a command's ``--json``
    stdout stream.
    """
    error_console.print(f"[red]Error:[/red] {message}")
    return typer.Exit(code=1)


@app.callback()
def _main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging."
    ),
) -> None:
    """tao-sentinel: a Bittensor watchtower built on the Taostats API."""
    _configure_logging(verbose)


@app.command()
def version() -> None:
    """Print the tao-sentinel version and exit."""
    console.print(f"tao-sentinel {__version__}")


@app.command()
def init(
    config: str = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to write the example configuration to.",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite an existing config file."
    ),
) -> None:
    """Write a commented example configuration file.

    By default writes ``./sentinel.yaml``. Refuses to overwrite an existing
    file unless ``--force`` is given.
    """
    import os

    if os.path.exists(config) and not force:
        console.print(
            f"[yellow]Refusing to overwrite existing file[/yellow] {config!r}. "
            "Use --force to overwrite."
        )
        raise typer.Exit(code=1)

    write_example_config(config)
    console.print(f"[green]Wrote example config to[/green] {config}")
    console.print(
        "Edit it to add your Taostats API key and watches, then run "
        "[bold]tao-sentinel watch --once[/bold] (or add [bold]--mock[/bold] "
        "to try it without a key)."
    )


@app.command()
def watch(
    config: str = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to the configuration file.",
    ),
    once: bool = typer.Option(
        False, "--once", help="Run a single evaluation and exit."
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic mock client (no network)."
    ),
    no_notify: bool = typer.Option(
        False,
        "--no-notify",
        help="Print alerts to the console only; skip configured "
        "Telegram/webhook notifiers.",
    ),
) -> None:
    """Run the alert engine against the configured watches.

    Loads the config, builds a client (mock or real), constructs the
    notifiers, and runs the :class:`~tao_sentinel.alerts.engine.WatchEngine`.
    With ``--once`` a single :meth:`run_once` is performed and the resulting
    alerts are dispatched to ALL configured notifiers (console, Telegram,
    webhook) exactly as in daemon mode -- use ``--no-notify`` for a
    console-only dry run; otherwise the engine polls forever at the
    configured interval.
    """
    # Imported here so `watch` is the only command that needs the alert stack.
    from .alerts.engine import WatchEngine
    from .alerts.notify import ConsoleNotifier, build_notifiers

    try:
        cfg = load_config(config)
    except _USER_ERRORS as exc:
        raise _fail(f"failed to load config {config!r}: {exc}") from exc

    client = make_client(_resolved_api_key(cfg), mock=mock)
    try:
        if no_notify:
            notifiers = [ConsoleNotifier(console=console)]
        else:
            notifiers = build_notifiers(cfg)
        engine = WatchEngine(client=client, config=cfg, notifiers=notifiers)

        if once:
            prev_state = engine.load_state()
            alerts, new_state = engine.run_once(prev_state)
            engine.save_state(new_state)

            if not alerts:
                console.print(
                    "[dim]No alerts: nothing changed beyond thresholds.[/dim]"
                )
                return

            # Dispatch through the engine so --once delivers to the SAME
            # notifier set as daemon mode (console rendering included via
            # the ConsoleNotifier that build_notifiers always adds).
            console.print(f"[bold]{len(alerts)} alert(s):[/bold]")
            engine.dispatch(alerts)
            return

        console.print(
            f"[green]Watching[/green] ({len(cfg.watches)} watch(es), polling every "
            f"{cfg.poll_interval_seconds}s). Press Ctrl+C to stop."
        )
        try:
            engine.run_forever()
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            pass
        # run_forever returns cleanly when its own KeyboardInterrupt handler
        # fires first, so print the farewell on BOTH paths (it previously
        # lived only in the except branch and never showed).
        console.print("\n[dim]Stopped.[/dim]")
    except TaostatsError as exc:
        raise _fail(str(exc)) from exc
    finally:
        client.close()


@app.command()
def portfolio(
    coldkey: str = typer.Argument(..., help="Coldkey ss58 address to value."),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic mock client (no network)."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the portfolio as JSON instead of a table."
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Optional config file to source the API key from.",
    ),
) -> None:
    """Value a coldkey's stake positions in TAO (and USD when available).

    Joins each alpha stake position with its subnet pool price and applies the
    TAO/USD spot price for a USD total. Positions whose subnet has no known
    pool price show no value and are excluded from the totals.
    """
    try:
        cfg = load_config(config) if config else None
    except _USER_ERRORS as exc:
        raise _fail(f"failed to load config {config!r}: {exc}") from exc

    client = make_client(_resolved_api_key(cfg), mock=mock)
    try:
        tracker = PortfolioTracker(client)
        result = tracker.get_portfolio(coldkey)
    except TaostatsError as exc:
        raise _fail(str(exc)) from exc
    finally:
        client.close()

    if json_out:
        _print_json(result.model_dump())
        return

    table = Table(
        title=f"Portfolio for {coldkey}",
        title_style="bold",
        header_style="bold cyan",
    )
    table.add_column("Netuid", justify="right")
    table.add_column("Hotkey", overflow="fold")
    table.add_column("Alpha staked", justify="right")
    table.add_column("Value (TAO)", justify="right")

    for pos in result.positions:
        value = "[dim]n/a[/dim]" if pos.value_tao is None else f"{pos.value_tao:,.4f}"
        table.add_row(
            str(pos.netuid),
            pos.hotkey,
            f"{pos.alpha_staked:,.4f}",
            value,
        )

    if not result.positions:
        table.add_row("-", "[dim]no positions[/dim]", "-", "-")

    console.print(table)
    console.print(
        f"[bold]Total:[/bold] {result.total_value_tao:,.4f} TAO"
        + (
            f"  ([green]${result.total_value_usd:,.2f}[/green]"
            f" @ ${result.tao_price_usd:,.2f}/TAO)"
            if result.total_value_usd is not None
            and result.tao_price_usd is not None
            else ""
        )
    )


@app.command()
def scan(
    netuid: Optional[int] = typer.Argument(
        None, help="Scan a single subnet (full validator detail). Omit for all."
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic mock client (no network)."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the reports as JSON instead of a table."
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Optional config file to source the API key from.",
    ),
) -> None:
    """Score subnet health and print a graded table.

    With a ``NETUID`` argument the scan pulls the subnet's validator set for
    full concentration/vtrust scoring. Without it, all subnets are scored from
    the subnet list alone to stay within the API rate limit.
    """
    try:
        cfg = load_config(config) if config else None
    except _USER_ERRORS as exc:
        raise _fail(f"failed to load config {config!r}: {exc}") from exc

    client = make_client(_resolved_api_key(cfg), mock=mock)
    try:
        scanner = SubnetScanner(client)
        reports = scanner.scan(netuid)
    except TaostatsError as exc:
        raise _fail(str(exc)) from exc
    finally:
        client.close()

    # The not-found guard runs BEFORE the --json branch so both output formats
    # agree on the exit code: an empty result (e.g. a nonexistent netuid) is an
    # error in both modes, reported to stderr so it never enters the JSON stream
    # on stdout. (finding 23)
    if not reports:
        target = f"netuid {netuid}" if netuid is not None else "any subnet"
        raise _fail(f"no health reports for {target}.")

    if json_out:
        _print_json([r.model_dump() for r in reports])
        return

    title = (
        f"Subnet health (netuid {netuid})"
        if netuid is not None
        else "Subnet health (all subnets)"
    )
    table = Table(title=title, title_style="bold", header_style="bold cyan")
    table.add_column("Netuid", justify="right")
    table.add_column("Name")
    table.add_column("Score", justify="right")
    table.add_column("Grade", justify="center")
    table.add_column("Warnings")

    for report in reports:
        grade_style = _GRADE_STYLES.get(report.grade, "white")
        warnings = (
            "; ".join(report.warnings) if report.warnings else "[dim]none[/dim]"
        )
        table.add_row(
            str(report.netuid),
            report.name or "[dim]?[/dim]",
            f"{report.score:.1f}",
            f"[{grade_style}]{report.grade}[/{grade_style}]",
            warnings,
        )

    console.print(table)


@app.command()
def serve(
    config: Optional[str] = typer.Option(
        None,
        "--config",
        "-c",
        help="Optional config file (for API key, watched coldkey, state).",
    ),
    port: int = typer.Option(8787, "--port", "-p", help="Port to bind."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind."),
    mock: bool = typer.Option(
        False, "--mock", help="Use the deterministic mock client (no network)."
    ),
) -> None:
    """Run the read-only web dashboard with uvicorn.

    Builds the FastAPI app via
    :func:`tao_sentinel.web.app.create_app` and serves it. Use ``--mock`` to
    run the dashboard without a Taostats API key.
    """
    import uvicorn

    from .web.app import create_app

    web_app = create_app(config_path=config, mock=mock)
    console.print(
        f"[green]Serving dashboard[/green] on http://{host}:{port}"
        + (" [dim](mock data)[/dim]" if mock else "")
    )
    uvicorn.run(web_app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    app()
