# agent/logger.py
"""
Logging setup for Trading Agent.

Creates two handlers:
  - Console: INFO level, colored by level
  - File:    DEBUG level, rotating 10MB x 5 files → logs/

Usage:
    from agent.logger import setup_logging
    setup_logging()               # uses LOG_LEVEL from .env
    setup_logging("DEBUG")        # explicit level
    setup_logging(log_file="test_agent")  # custom filename → logs/test_agent.log
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR      = Path("logs")
MAX_BYTES    = 10 * 1024 * 1024   # 10 MB
BACKUP_COUNT = 5                   # keep 5 rotated files → max 60 MB total

# Simple format for console
CONSOLE_FMT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
# Detailed format for file (includes module + line number)
FILE_FMT    = "%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s"
DATE_FMT    = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    level:    str = "INFO",
    log_file: str = "agent",
) -> None:
    """
    Configure root logger with console + rotating file handler.

    Args:
        level:    Log level for console (DEBUG / INFO / WARNING / ERROR)
        log_file: Base filename without extension → logs/{log_file}.log
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # On Windows the default console encoding (cp1251/cp866) cannot encode
    # emoji/Unicode characters used in log messages → UnicodeEncodeError.
    # Reconfigure stdout to UTF-8 with a safe fallback if that fails.
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Create logs/ directory
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{log_file}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)   # root captures everything; handlers filter

    # Remove existing console/file handlers to avoid duplicates on re-call,
    # but preserve any custom handlers (e.g. GUI queue handler for live log streaming).
    root.handlers = [
        h for h in root.handlers
        if not isinstance(h, (logging.StreamHandler, RotatingFileHandler))
        or getattr(h, '_preserve', False)
    ]

    # ── Console handler ───────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(_ColorFormatter(CONSOLE_FMT, DATE_FMT))
    root.addHandler(console)

    # ── Rotating file handler ─────────────────────────────
    try:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes    = MAX_BYTES,
            backupCount = BACKUP_COUNT,
            encoding    = "utf-8",
        )
        file_handler.setLevel(logging.DEBUG)   # file always gets DEBUG
        file_handler.setFormatter(logging.Formatter(FILE_FMT, DATE_FMT))
        root.addHandler(file_handler)
    except Exception as e:
        logging.warning(f"[Logger] Could not create file handler: {e}")

    # Suppress noisy third-party loggers
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        f"Logging started | level={level.upper()} | file={log_path}"
    )


# ─────────────────────────────────────────
# COLORED CONSOLE FORMATTER
# ─────────────────────────────────────────

class _ColorFormatter(logging.Formatter):
    """Adds ANSI color codes to console output by log level."""

    COLORS = {
        logging.DEBUG:    "\033[37m",    # gray
        logging.INFO:     "\033[0m",     # default
        logging.WARNING:  "\033[33m",    # yellow
        logging.ERROR:    "\033[31m",    # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, self.RESET)
        msg   = super().format(record)
        # Only color if output is a real terminal (not piped)
        if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            return f"{color}{msg}{self.RESET}"
        return msg
