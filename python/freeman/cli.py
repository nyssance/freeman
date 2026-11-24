from __future__ import annotations

import importlib.metadata

import click


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main():
    """Freeman command line interface."""


@main.command()
def version():
    """Show the Freeman version."""
    click.echo(f"Freeman version: {importlib.metadata.version('freeman')}")
