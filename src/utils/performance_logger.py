import time
from contextlib import contextmanager
from .logger import logger
from .telemetry import start_span


@contextmanager
def log_timing(operation: str):
    start_time = time.time()
    with start_span(operation):
        logger.info(f"⏱️ START | {operation}")
        try:
            yield
        finally:
            duration = time.time() - start_time
            logger.info(f"✅ END | {operation} | {duration:.2f}s")
