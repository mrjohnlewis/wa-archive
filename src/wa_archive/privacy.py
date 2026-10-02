"""Keep message contents out of logs, errors and tracebacks.

Exception messages can carry data (a file path containing a contact's name, a
value that failed to parse). By default we print only the exception type and
the code location; `--debug` shows full details for synthetic-data debugging.
"""

from __future__ import annotations

import logging
import sys
import traceback

log = logging.getLogger("wa_archive")


def setup_logging(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    sys.excepthook = _debug_excepthook if debug else _redacted_excepthook


def redacted(exc: BaseException) -> str:
    """Describe an exception without its message: type plus innermost frame."""
    tb = traceback.extract_tb(exc.__traceback__)
    where = f" at {tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno}" if tb else ""
    return f"{type(exc).__name__}{where}"


def _redacted_excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        print("\nInterrupted.", file=sys.stderr)
        return
    frames = traceback.extract_tb(tb)
    print(f"Error: {exc_type.__name__} (details hidden; re-run with --debug only on synthetic data)",
          file=sys.stderr)
    for fr in frames[-5:]:
        print(f"  {fr.filename.rsplit('/', 1)[-1]}:{fr.lineno} in {fr.name}", file=sys.stderr)


def _debug_excepthook(exc_type, exc, tb) -> None:
    traceback.print_exception(exc_type, exc, tb)
