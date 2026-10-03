"""Optional, read-only tegrastats telemetry with receipt-time provenance.

Only the collector's own child is signalled. No device settings are changed.
The parser reports EMC activity percentages, never inferred bandwidth.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import threading
import time

from .lifecycle import defer_interrupts


_FIELDS = ('gpu_utilization_percent', 'gpu_frequency_mhz',
           'emc_activity_percent', 'emc_frequency_mhz')
_PAYLOAD = re.compile(r'\s*(?:(?P<percent>[+-]?\d+(?:\.\d+)?)\s*%)?\s*'
                      r'(?:@\s*(?P<frequency>\[[^\]]*\]|[+-]?\d+(?:\.\d+)?))?\s*')


def _unavailable(reason):
    return dict.fromkeys(_FIELDS, None) | {'availability': dict.fromkeys(_FIELDS, reason)}


def parse_tegrastats(line):
    """Parse only explicit GR3D/EMC fields; missing and invalid values are null.

    GPU frequencies are a list (one entry per reported GPC). EMC frequency is
    scalar. Availability maps each metric to None or its missing/invalid reason.
    """
    values = _unavailable('field not reported')
    for marker, percent_key, frequency_key, array_allowed in (
            ('GR3D_FREQ', _FIELDS[0], _FIELDS[1], True),
            ('EMC_FREQ', _FIELDS[2], _FIELDS[3], False)):
        field = re.search(r'\b' + marker + r'\s+([^\r\n]*)', line)
        if field is None:
            continue
        # A following tegrastats field starts with a word, unlike the numeric
        # payload. Keep invalid numeric text so malformed values are rejected.
        payload = re.split(r'\s+[A-Za-z_][^\s]*', field.group(1), maxsplit=1)[0]
        match = _PAYLOAD.fullmatch(payload)
        if match is None or not any(match.groupdict().values()):
            for key in (percent_key, frequency_key):
                values['availability'][key] = 'invalid field format'
            continue
        percent = match.group('percent')
        if percent is not None:
            number = float(percent)
            if math.isfinite(number) and 0 <= number <= 100:
                values[percent_key] = number
                values['availability'][percent_key] = None
            else:
                values['availability'][percent_key] = 'percentage outside 0..100'
        else:
            values['availability'][percent_key] = 'percentage not reported'
        frequency = match.group('frequency')
        if frequency is None:
            values['availability'][frequency_key] = 'frequency not reported'
            continue
        try:
            is_array = frequency.startswith('[')
            if is_array and not array_allowed:
                raise ValueError('EMC requires a scalar frequency')
            numbers = [float(item.strip()) for item in
                       (frequency[1:-1].split(',') if is_array else [frequency])]
            if not numbers or any(not math.isfinite(item) or item < 0 for item in numbers):
                raise ValueError('invalid frequency')
        except ValueError:
            values['availability'][frequency_key] = 'invalid nonnegative frequency'
        else:
            values[frequency_key] = numbers if array_allowed else numbers[0]
            values['availability'][frequency_key] = None
    return values


class TegrastatsCollector:
    """Own one optional child and a bounded reader, retaining only latest values.

    stdout is persisted as JSONL with local monotonic receipt times, not device
    generation times. stderr is preserved in ``raw_output + '.stderr.log'``.
    Files are created exclusively. The collector is single-use.
    """
    MAX_LINE_BYTES = 64 * 1024
    READ_BYTES = 8192
    TERMINATE_TIMEOUT_SECONDS = 1.0

    def __init__(self, raw_output: Path, interval_seconds: float,
                 enabled: bool = False, executable: str | None = None):
        interval_seconds = float(interval_seconds)
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError('tegrastats interval must be finite and positive')
        if interval_seconds > 2_147_483.647:
            raise ValueError('tegrastats interval exceeds signed millisecond range')
        milliseconds = max(1, math.ceil(interval_seconds * 1000))
        if milliseconds > 2_147_483_647:
            raise ValueError('tegrastats interval exceeds signed millisecond range')
        self.raw_output = Path(raw_output)
        self.stderr_path = Path(str(self.raw_output) + '.stderr.log')
        self.interval_seconds = milliseconds / 1000
        self.enabled = enabled
        self.executable = executable
        self._interval_ms = milliseconds
        self._stale_ns = int(max(3 * self.interval_seconds, 1) * 1_000_000_000)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._process = None
        self._thread = None
        self._raw_stream = None
        self._stderr_stream = None
        self._entered = False
        self._state = 'disabled' if not enabled else 'not_started'
        self._error = None
        self._returncode = None
        self._sample_id = None
        self._received_ns = None
        self._values = _unavailable(self._state)
        self._dropped_lines = 0

    def __enter__(self):
        if self._entered:
            raise RuntimeError('TegrastatsCollector cannot be entered twice')
        self._entered = True
        if not self.enabled:
            return self
        executable = self.executable or shutil.which('tegrastats')
        if executable is None:
            self._state = 'executable_not_found'
            return self
        try:
            with defer_interrupts():
                self._raw_stream = self.raw_output.open('x', encoding='utf-8')
                self._stderr_stream = self.stderr_path.open('xb')
                try:
                    self._process = subprocess.Popen(
                        [executable, '--interval', str(self._interval_ms)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, bufsize=0)
                except FileNotFoundError:
                    self._state = 'executable_not_found'
                except PermissionError:
                    self._state = 'permission_denied'
                except OSError as exc:
                    self._state = 'start_failed'
                    self._error = type(exc).__name__ + ': ' + str(exc)
                if self._process is not None:
                    self._state = 'running'
                    self._thread = threading.Thread(target=self._read,
                                                    name='tegrastats-reader', daemon=True)
                    self._thread.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def _record(self, raw, received):
        line = raw.decode('utf-8', errors='replace')
        values = parse_tegrastats(line)
        with self._lock:
            sample_id = (self._sample_id or 0) + 1
        self._raw_stream.write(json.dumps({'sample_id': sample_id,
                                          'received_monotonic_ns': received,
                                          'raw': line}, allow_nan=False) + '\n')
        self._raw_stream.flush()
        with self._lock:
            self._sample_id = sample_id
            self._received_ns = received
            self._values = values

    def _read(self):
        # Fixed-size reads plus a capped partial stdout line; no sample queue.
        pending = bytearray()
        pending_received_ns = None
        discarding = False
        try:
            with selectors.DefaultSelector() as selector:
                for name in ('stdout', 'stderr'):
                    stream = getattr(self._process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() and not self._stop.is_set():
                    for key, _ in selector.select(timeout=0.05):
                        try:
                            chunk = os.read(key.fd, self.READ_BYTES)
                            received = time.monotonic_ns()
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            if key.data == 'stdout' and pending and not discarding:
                                self._record(pending, pending_received_ns)
                                pending.clear()
                            continue
                        if key.data == 'stderr':
                            self._stderr_stream.write(chunk)
                            self._stderr_stream.flush()
                            continue
                        pending_received_ns = received
                        for fragment_index, fragment in enumerate(chunk.split(b'\n')):
                            if fragment_index:
                                if not discarding:
                                    self._record(pending, received)
                                pending.clear()
                                discarding = False
                            if not discarding:
                                if len(pending) + len(fragment) > self.MAX_LINE_BYTES:
                                    pending.clear()
                                    discarding = True
                                    with self._lock:
                                        self._dropped_lines += 1
                                else:
                                    pending.extend(fragment)
        except (OSError, ValueError) as exc:
            with self._lock:
                self._error = type(exc).__name__ + ': ' + str(exc)
                self._state = 'reader_failed'

    def close(self):
        """TERM/wait/KILL only the owned child, then join and close the reader."""
        with defer_interrupts():
            process = self._process
            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=self.TERMINATE_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        process.wait()
                self._returncode = process.wait()
            if self._thread is not None and self._thread.ident is not None:
                # Give EOF a chance to persist the final partial line and stderr.
                # If inherited descriptors remain open, explicitly stop the
                # nonblocking reader instead of waiting on an unrelated owner.
                self._thread.join(timeout=1.0)
            self._stop.set()
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join()
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    stream.close()
            for stream in (self._raw_stream, self._stderr_stream):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError as exc:
                        with self._lock:
                            self._error = self._error or type(exc).__name__ + ': ' + str(exc)
                            self._state = 'close_failed'
            with self._lock:
                if self._state == 'running':
                    self._state = 'closed'

    def owned_process_ids(self):
        """Currently living owned child; never retain dead PIDs for discovery exclusion."""
        process = self._process
        return (process.pid,) if process is not None and process.poll() is None else ()

    def snapshot(self, now_ns=None):
        """Copy latest sample; duplicate sample_id means no new received line.

        Unavailable snapshots clear metrics but retain last receipt provenance.
        The age is local time since receipt, not device generation age.
        """
        now = time.monotonic_ns() if now_ns is None else int(now_ns)
        with self._lock:
            state = self._state
            returncode = self._returncode
            if state == 'running' and self._process is not None:
                returncode = self._process.poll()
                if returncode is not None:
                    state = 'exited'
            received = self._received_ns
            age = now - received if received is not None else None
            reason = None
            if state != 'running':
                reason = state
            elif self._error:
                reason = 'reader_failed'
            elif received is None:
                reason = 'no_sample_received'
            elif age < 0:
                reason = 'receipt_time_in_future'
            elif age > self._stale_ns:
                reason = 'stale_sample'
            elif all(self._values[key] is None for key in _FIELDS):
                reason = 'no_supported_metrics'
            values = _unavailable(reason) if reason else copy.deepcopy(self._values)
            return {'available': reason is None, 'reason': reason,
                    'sample_id': self._sample_id, 'received_monotonic_ns': received,
                    'age_ns': age, 'values': values,
                    'status': {'state': state, 'returncode': returncode,
                               'error': self._error, 'dropped_stdout_lines': self._dropped_lines,
                               'stderr_path': str(self.stderr_path)}}
