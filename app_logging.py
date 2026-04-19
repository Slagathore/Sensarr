import logging
import threading
from collections import deque

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_BUFFER: deque[str] = deque(maxlen=400)
_LOG_LOCK = threading.Lock()
_LOGGING_CONFIGURED = False


class InMemoryLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()

        with _LOG_LOCK:
            _LOG_BUFFER.append(message)


def configure_logging() -> None:
    global _LOGGING_CONFIGURED
    root_logger = logging.getLogger()
    if _LOGGING_CONFIGURED:
        return

    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    memory_handler = InMemoryLogHandler()
    memory_handler.setFormatter(formatter)
    root_logger.addHandler(memory_handler)

    _LOGGING_CONFIGURED = True


def get_recent_logs() -> list[str]:
    with _LOG_LOCK:
        return list(_LOG_BUFFER)
