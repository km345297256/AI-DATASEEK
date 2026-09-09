import logging
import sys
import re
from app.core.config import get_settings


class OfficeLeaseLogFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's access tuple contains the complete request path. Capability
        # URLs must not become long-lived credentials in ordinary runtime logs.
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"(/office-viewer/(?:private/(?:frame|files|status)|leases)/)[A-Za-z0-9_-]{43}",
                r"\1[redacted]", value) if isinstance(value, str) else value for value in record.args)
        return True

def setup_logging():
    """
    Configure the application logging system

    Sets up log levels, formatters, and handlers for both console and file output.
    Ensures proper log rotation to prevent log files from growing too large.
    """
    # Get configuration
    settings = get_settings()

    # Get root logger
    root_logger = logging.getLogger()

    # Set root log level
    log_level = getattr(logging, settings.log_level)
    root_logger.setLevel(log_level)

    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)

    # Add handlers to root logger
    root_logger.addHandler(console_handler)

    # Disable verbose logging for pymongo
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("sse_starlette.sse").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").addFilter(OfficeLeaseLogFilter())

    # Log initialization complete
    root_logger.info("Logging system initialized - Console and file logging active")
