import logging
import sys

# ANSI escape sequences for some colors
COLORS = {
    "HEADER": "\033[95m",
    "OKBLUE": "\033[94m",
    "OKCYAN": "\033[96m",
    "OKGREEN": "\033[92m",
    "WARNING": "\033[93m",
    "FAIL": "\033[91m",
    "ENDC": "\033[0m",
    "BOLD": "\033[1m",
    "UNDERLINE": "\033[4m",
}

DEFAULT_LOG_LEVEL = logging.INFO


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: COLORS["OKBLUE"],
        logging.INFO: COLORS["OKGREEN"],
        logging.WARNING: COLORS["WARNING"],
        logging.ERROR: COLORS["FAIL"],
        logging.CRITICAL: COLORS["FAIL"]
    }

    def format(self, record):
        if sys.stdout.isatty():
            level_color = self.LEVEL_COLORS.get(record.levelno, COLORS["ENDC"])
            record.levelname = f"{level_color}{record.levelname}{COLORS['ENDC']}"
        return super().format(record)


def get_logger(name, level=None):
    logger = logging.getLogger(name)

    logger.setLevel(level if level is not None else DEFAULT_LOG_LEVEL)

    handler = logging.StreamHandler(sys.stdout)
    formatter = ColorFormatter('%(asctime)s %(levelname)-8s [%(name)s] %(message)s', datefmt='%m/%d %H:%M:%S')
    handler.setFormatter(formatter)

    # Ensure we're not adding multiple handlers to the same logger
    if not logger.handlers:
        logger.addHandler(handler)

    return logger


if __name__ == "__main__":
    # Example usage, the log level can be adjusted here
    logger = get_logger(__name__, level=logging.DEBUG)  # Set to DEBUG for testing

    # Test the logger
    logger.debug("This is a debug message")
    logger.info("This is an info message")
    logger.warning("This is a warning message")
    logger.error("This is an error message")
    logger.critical("This is a critical message")
    logger.fatal("This is a fatal message")  # FATAL is an alias for CRITICAL
