"""
Functions related to logging.
"""

import logging
import threading

DEFAULT_FORMATTER = logging.Formatter('[%(asctime)s][%(name)s][%(levelname)s][%(filename)s:%(lineno)d]: %(message)s')
LOCALS = threading.local()

MAIN_LOGGER = logging.getLogger()

def get_logger() -> logging.Logger:
    return MAIN_LOGGER

def setup_root_logger():
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARN)
    logging.getLogger("docker").setLevel(logging.WARN)
    MAIN_LOGGER.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setFormatter(DEFAULT_FORMATTER)
    MAIN_LOGGER.addHandler(handler)

    return MAIN_LOGGER