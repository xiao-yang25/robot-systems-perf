"""Delay user cancellation while owned processes/streams are being released."""
from contextlib import contextmanager
import signal
import threading


@contextmanager
def defer_interrupts():
    # Python signal handlers run in the main thread. Non-main callers must not
    # replace process-wide handlers; their cleanup is not interrupted by them.
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending = []
    saved = {}

    def remember(signum, frame):
        pending.append(signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            saved[signum] = signal.getsignal(signum)
            signal.signal(signum, remember)
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)
    if pending:
        raise KeyboardInterrupt('interrupted during cleanup by signal ' + str(pending[0]))
