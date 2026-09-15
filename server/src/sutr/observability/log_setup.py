"""Install the process-wide logging configuration.

Called once from `main.py` before any other sutr module initializes a logger,
which is why `main.py` keeps its deliberately late imports.
"""

import logging

from sutr.observability.log_format import JsonFormatter, TextFormatter

FORMATS = ("json", "text")


def configure_logging(level: str = "INFO", log_format: str = "json", service: str = "sutr") -> None:
    """Replace the root handler with one that formats as configured.

    Idempotent: re-running swaps the formatter rather than stacking handlers,
    so a test that reconfigures logging does not double every line.
    """
    normalized = (log_format or "json").lower()
    if normalized not in FORMATS:
        normalized = "json"
    formatter = JsonFormatter(service=service) if normalized == "json" else TextFormatter()

    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))

    handler = next(
        (h for h in root.handlers if getattr(h, "_sutr_handler", False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()
        handler._sutr_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    handler.setFormatter(formatter)
