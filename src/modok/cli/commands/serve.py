# @spec WH-SERVE-001, WH-SERVE-002, WH-SERVE-003, WH-SERVE-004, WH-SERVE-005, WH-SERVE-006

from __future__ import annotations

import click


@click.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=4242, show_default=True)
def serve_command(host: str, port: int) -> None:
    """Start the webhook receiver."""
    from modok.webhook.server import serve_main
    serve_main(["--host", host, "--port", str(port)])
