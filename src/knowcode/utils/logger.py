"""Standard logging configuration for KnowCode."""

import logging
import sys


def get_logger(name: str) -> logging.Logger:
    """Get a standardized logger configured for console output.

    Args:
        name: Logger name to retrieve.

    Returns:
        A logger configured with a stream handler if none exists.
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        
    return logger
