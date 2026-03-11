import time
import functools
from .logger import logger

def log_execution_time(func):
    """Decorator to log the execution time of an async function."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            return result
        finally:
            end_time = time.time()
            duration = (end_time - start_time) * 1000
            logger.info(f"⏱️ [{func.__name__}] executed in {duration:.2f}ms")
    return wrapper
