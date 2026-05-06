"""modok quine subcommands: start | stop | status."""
# @spec CLI-QS-001, CLI-QS-002, CLI-QS-003, CLI-QS-004, CLI-QS-005
# @spec CLI-QSTOP-001, CLI-QSTOP-002, CLI-QSTOP-003, CLI-QSTOP-004
# @spec CLI-QSTAT-001, CLI-QSTAT-002

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

import click

from modok.cli.config import ModokConfig
from modok.quine.client import QuineClient

QUINE_PID_PATH = Path.home() / ".modok" / "quine.pid"
QUINE_START_TIMEOUT = 30  # seconds
QUINE_STOP_TIMEOUT = 10  # seconds


@click.group("quine")
def quine_cmd() -> None:
    """Manage the Quine process."""


@quine_cmd.command("start")
def quine_start() -> None:
    config = ModokConfig.load()
    client = QuineClient(base_url=config.quine.url)

    if asyncio.run(client.ping()):
        click.echo(f"Quine is already running at {config.quine.url}", err=True)
        raise SystemExit(0)

    jar_path = Path(config.quine.jar)
    if not jar_path.exists():
        click.echo(
            f"Quine JAR not found at {jar_path} — run the setup guide to download it",
            err=True,
        )
        raise SystemExit(1)

    proc = subprocess.Popen(
        ["java", f"-Dconfig.file={Path.home()}/.modok/quine.conf", "-jar", str(jar_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    QUINE_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUINE_PID_PATH.write_text(str(proc.pid))

    deadline = time.monotonic() + QUINE_START_TIMEOUT
    while time.monotonic() < deadline:
        if asyncio.run(client.ping()):
            raise SystemExit(0)
        time.sleep(1)

    click.echo("Quine did not become ready within 30s", err=True)
    raise SystemExit(2)


@quine_cmd.command("stop")
def quine_stop() -> None:
    if not QUINE_PID_PATH.exists():
        click.echo("Quine is not running (no PID file found)", err=True)
        raise SystemExit(1)

    pid = int(QUINE_PID_PATH.read_text().strip())

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(
            f"process {pid} not found — Quine may have crashed; check ~/.modok/quine.log",
            err=True,
        )
        raise SystemExit(2)

    if _wait_for_exit(pid, QUINE_STOP_TIMEOUT):
        QUINE_PID_PATH.unlink(missing_ok=True)
        raise SystemExit(0)
    else:
        click.echo("Quine did not stop within 10s — PID file left in place", err=True)
        raise SystemExit(2)


@quine_cmd.command("status")
def quine_status() -> None:
    config = ModokConfig.load()
    client = QuineClient(base_url=config.quine.url)
    if asyncio.run(client.ping()):
        click.echo("running")
    else:
        click.echo("stopped")


def _wait_for_exit(pid: int, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)  # probe: process still exists?
        except ProcessLookupError:
            return True
        time.sleep(0.5)
    return False
