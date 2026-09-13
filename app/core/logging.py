"""Configure only application logs, leaving server/library logging unchanged."""

import logging


def configure_logging() -> None:
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
