"""Enables `python -m dossier.cli`. The Typer app lives in `main.py` (which
is also the `dossier` console-script entry point declared in pyproject)."""

from dossier.cli.main import app

if __name__ == "__main__":
    app()
