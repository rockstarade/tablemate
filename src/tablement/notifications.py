"""Post-snipe notifications: Rich console output + macOS notification."""

from __future__ import annotations

import subprocess
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from tablement.models import SnipeResult

console = Console()


def display_result(result: SnipeResult) -> None:
    """Display the snipe result with Rich formatting."""
    if result.success and result.error and "DRY RUN" in result.error:
        _display_dry_run(result)
    elif result.success:
        _display_success(result)
    else:
        _display_failure(result)


def _display_success(result: SnipeResult) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    if result.slot:
        table.add_row("Time", result.slot.date.start)
        table.add_row("Type", result.slot.config.type)
    if result.resy_token:
        table.add_row("Confirmation", result.resy_token)
    table.add_row("Attempts", str(result.attempts))
    table.add_row("Elapsed", f"{result.elapsed_seconds:.3f}s")

    console.print(Panel(table, title="RESERVATION CONFIRMED", border_style="green"))
    _macos_notify(
        "Reservation Confirmed!",
        f"{result.slot.date.start if result.slot else 'Unknown time'} — "
        f"Booked in {result.elapsed_seconds:.1f}s",
    )


def _display_dry_run(result: SnipeResult) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    if result.slot:
        table.add_row("Would book", result.slot.date.start)
        table.add_row("Type", result.slot.config.type)
    table.add_row("Attempts", str(result.attempts))
    table.add_row("Elapsed", f"{result.elapsed_seconds:.3f}s")

    console.print(Panel(table, title="DRY RUN — No booking made", border_style="yellow"))


def _display_failure(result: SnipeResult) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    table.add_row("Error", result.error or "Unknown")
    table.add_row("Attempts", str(result.attempts))
    table.add_row("Elapsed", f"{result.elapsed_seconds:.3f}s")

    console.print(Panel(table, title="SNIPE FAILED", border_style="red"))
    _macos_notify("Snipe Failed", result.error or "Unknown error")


def _macos_notify(title: str, message: str) -> None:
    """Send a macOS notification via osascript."""
    if sys.platform != "darwin":
        return
    try:
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # Non-critical
