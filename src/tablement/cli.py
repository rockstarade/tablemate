"""Click CLI commands for Tablement."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from tablement.api import ResyApiClient
from tablement.auth import AuthManager
from tablement.config import load_snipe_config
from tablement.errors import TablementError
from tablement.notifications import display_result
from tablement.scheduler import PrecisionScheduler
from tablement.sniper import ReservationSniper
from tablement.venue import VenueLookup

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def main(verbose: bool) -> None:
    """Tablement: Resy reservation sniper for accessibility."""
    _setup_logging(verbose)


@main.command()
def configure() -> None:
    """Store Resy credentials securely in macOS Keychain."""
    email = click.prompt("Resy email")
    password = click.prompt("Resy password", hide_input=True)

    auth = AuthManager()
    auth.store_credentials(email, password)

    console.print("[green]Credentials stored.[/green] Verifying...")

    async def _verify() -> None:
        async with ResyApiClient() as client:
            resp = await client.authenticate(email, password)
            console.print(f"[green]Authenticated as {resp.first_name} {resp.last_name}[/green]")
            if resp.payment_methods:
                pm = resp.payment_methods[0]
                console.print(f"Payment method: {pm.display or f'id={pm.id}'}")
            else:
                console.print("[yellow]Warning: No payment methods on account.[/yellow]")

    try:
        asyncio.run(_verify())
    except Exception as e:
        console.print(f"[red]Verification failed: {e}[/red]")
        console.print("Credentials were still saved. You can fix them with 'tablement configure'.")


@main.command()
def test_auth() -> None:
    """Test stored credentials and show account info."""
    auth = AuthManager()
    try:
        credentials = auth.load_credentials()
    except TablementError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    async def _test() -> None:
        async with ResyApiClient() as client:
            token, pm_id = await auth.login(client, credentials)
            console.print("[green]Authentication successful![/green]")
            console.print(f"Token: {token[:20]}...")
            console.print(f"Payment method ID: {pm_id}")

    try:
        asyncio.run(_test())
    except Exception as e:
        console.print(f"[red]Auth test failed: {e}[/red]")
        sys.exit(1)


@main.command()
@click.argument("url")
def lookup(url: str) -> None:
    """Look up venue ID and booking policy from a Resy restaurant URL."""
    venue_lookup = VenueLookup()

    async def _lookup() -> None:
        import httpx as _httpx

        venue_id, venue_name = await venue_lookup.from_url(url)

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Venue ID", str(venue_id))
        if venue_name:
            table.add_row("Name", venue_name)

        # Try to scrape booking policy
        async with _httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            })
            policy = venue_lookup.scrape_booking_policy(resp.text)

        if policy:
            table.add_row("Days ahead", str(policy.days_ahead))
            table.add_row("Drop time", f"{policy.hour:02d}:{policy.minute:02d}")
            table.add_row("Timezone", policy.timezone)
        else:
            table.add_row("Policy", "[yellow]Could not auto-detect. Enter manually in config.[/yellow]")

        console.print(Panel(table, title="Venue Info"))

        # Print a starter config
        console.print("\n[bold]Starter config:[/bold]")
        config_lines = [
            f"venue_id: {venue_id}",
            f'venue_name: "{venue_name or "Unknown"}"',
            "party_size: 2",
            f'date: "{datetime.now().strftime("%Y-%m-%d")}"  # Change to your target date',
            "",
            "time_preferences:",
            '  - time: "19:00"',
            "",
            "drop_time:",
        ]
        if policy:
            config_lines.extend([
                f"  hour: {policy.hour}",
                f"  minute: {policy.minute}",
                f'  timezone: "{policy.timezone}"',
                f"  days_ahead: {policy.days_ahead}",
            ])
        else:
            config_lines.extend([
                "  hour: 10        # When reservations drop",
                "  minute: 0",
                '  timezone: "America/New_York"',
                "  days_ahead: 30  # How many days in advance",
            ])

        console.print("\n".join(config_lines))

    try:
        asyncio.run(_lookup())
    except TablementError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Lookup failed: {e}[/red]")
        sys.exit(1)


@main.command()
@click.option("--port", default=8422, help="Port to serve on.")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
def web(port: int, host: str) -> None:
    """Launch the Tablement web UI in your browser."""
    try:
        import uvicorn
        from tablement.web.app import create_app
    except ImportError:
        console.print(
            "[red]Web dependencies not installed.[/red] "
            "Run: pip install tablement[web]"
        )
        sys.exit(1)

    app = create_app()
    console.print(f"[bold green]Tablement Web UI[/bold green] -> http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


@main.command()
@click.argument("config_file", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Find and select slots without booking.")
def snipe(config_file: str, dry_run: bool) -> None:
    """Execute a reservation snipe from a YAML config file."""
    try:
        config = load_snipe_config(config_file)
    except TablementError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    scheduler = PrecisionScheduler()
    drop_time = scheduler.calculate_drop_datetime(config)
    now = datetime.now(drop_time.tzinfo)

    # Display snipe info
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Venue", config.venue_name or str(config.venue_id))
    table.add_row("Date", config.date.isoformat())
    table.add_row("Party size", str(config.party_size))
    table.add_row("Drop time", drop_time.strftime("%Y-%m-%d %H:%M:%S %Z"))
    for i, pref in enumerate(config.time_preferences, 1):
        seating = f" ({pref.seating_type})" if pref.seating_type else ""
        table.add_row(f"Preference #{i}", f"{pref.time.strftime('%H:%M')}{seating}")

    if dry_run:
        table.add_row("Mode", "[yellow]DRY RUN[/yellow]")

    console.print(Panel(table, title="Snipe Configuration"))

    if drop_time < now:
        console.print("[yellow]Drop time is in the past. Running immediately...[/yellow]")
    else:
        wait = drop_time - now
        hours, remainder = divmod(int(wait.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        console.print(f"Snipe starts in [bold]{hours}h {minutes}m {seconds}s[/bold]")

    # Check NTP offset
    offset = scheduler.check_ntp_offset()
    if offset is not None:
        if abs(offset) > 0.5:
            console.print(
                f"[red]Warning: System clock is off by {offset:.1f}s! "
                f"Consider syncing with NTP.[/red]"
            )
        else:
            console.print(f"Clock offset: {offset*1000:.0f}ms (OK)")

    # Execute
    sniper = ReservationSniper(scheduler=scheduler)
    try:
        result = asyncio.run(sniper.execute(config, dry_run=dry_run))
        display_result(result)
        if not result.success:
            sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Snipe cancelled.[/yellow]")
        sys.exit(130)
    except TablementError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        sys.exit(1)
