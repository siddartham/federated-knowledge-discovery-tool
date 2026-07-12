"""Enables `python -m columbo_py.cli`. The Typer app lives in `main.py` (which
is also the `columbo` console-script entry point declared in pyproject)."""

from columbo_py.cli.main import app

if __name__ == "__main__":
    app()
