import logging

from rich.console import Console
from rich.logging import RichHandler

from decoder.config import settings

_console: Console | None = None


def get_console() -> Console:
    global _console
    if _console is None:
        _console = Console()
    return _console


def configure_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=(level or settings.log_level).upper(),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=get_console(), rich_tracebacks=True, markup=True)],
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
