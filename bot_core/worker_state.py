"""Thread ownership and generation fences for synchronous workers.

Standard library only. Importing this module starts no threads and performs no IO.
The caller retains its existing diagnostic dictionaries; all owned writes and
cleanup go through the same lock so a retired task cannot erase its successor.
"""
from __future__ import annotations

import threading


class WorkerRetiredError(RuntimeError):
    """A task belongs to an executor generation that has already been retired."""


class WorkerState:
    def __init__(self):
        self._lock = threading.RLock()
        self._generations = {}
        self._runs = {}
        self._serial = 0
        self.local = threading.local()

    def generation(self, key):
        with self._lock:
            return self._generations.get(key, 0)

    def begin(self, key, generation, thread_ids, thread_names):
        with self._lock:
            if generation != self._generations.get(key, 0):
                raise WorkerRetiredError(f'worker_generation_retired:{key}')
            self._serial += 1
            token = self._serial
            tid = threading.get_ident()
            self._runs[key] = (token, generation, tid)
            # Assign to the actual dictionaries, even when initially empty.
            thread_ids[key] = tid
            thread_names[key] = threading.current_thread().name
            self.local.worker_key = key
            self.local.run_token = token
            return token

    def _owns(self, key, token):
        row = self._runs.get(key)
        return bool(row and token and row[0] == token
                    and row[1] == self._generations.get(key, 0)
                    and row[2] == threading.get_ident())

    def is_owner(self, key=''):
        with self._lock:
            actual_key = str(getattr(self.local, 'worker_key', '') or '')
            return bool((not key or key == actual_key)
                        and self._owns(actual_key, getattr(self.local, 'run_token', None)))

    def retired_current(self):
        return bool(getattr(self.local, 'run_token', None) and not self.is_owner())

    def assert_live(self):
        if self.retired_current():
            raise WorkerRetiredError('worker_generation_retired:' + str(self.local.worker_key))

    def publish(self, key, target, data, *, merge=False):
        with self._lock:
            token = getattr(self.local, 'run_token', None)
            if not self._owns(key, token):
                return {}
            row = dict(target.get(key) or {}) if merge else {}
            row.update(data)
            row['run_token'] = token
            row['worker_generation'] = self._generations.get(key, 0)
            target[key] = row
            return dict(row)

    def clear(self, key, target, *, expected_token=None):
        """Owner cleanup, or compare-and-clear of an explicitly observed record."""
        with self._lock:
            token = getattr(self.local, 'run_token', None)
            if expected_token is None:
                if not self._owns(key, token):
                    return False
                expected_token = token
            row = target.get(key)
            if not row or row.get('run_token') != expected_token:
                return False
            target.pop(key, None)
            return True

    def invalidate(self, key, heartbeats, inflight, thread_ids, thread_names):
        with self._lock:
            self._generations[key] = self._generations.get(key, 0) + 1
            self._runs.pop(key, None)
            for mapping in (heartbeats, inflight, thread_ids, thread_names):
                mapping.pop(key, None)
            return self._generations[key]

    def finish(self, key, token, heartbeats, inflight, thread_ids, thread_names, on_finish=None):
        with self._lock:
            owned = self._owns(key, token)
            try:
                if owned:
                    try:
                        if on_finish is not None:
                            on_finish()
                    finally:
                        self.clear(key, heartbeats)
                        self.clear(key, inflight)
                        thread_ids.pop(key, None)
                        thread_names.pop(key, None)
                        self._runs.pop(key, None)
            finally:
                # A retired task can clear its own TLS, never a successor's maps.
                if getattr(self.local, 'run_token', None) == token:
                    self.local.worker_key = ''
                    self.local.run_token = None
            return owned


def timeout_context(reason, heartbeat=None, active_request=None):
    """Freeze the operation that timed out before recovery overwrites diagnostics."""
    hb = dict(heartbeat or {})
    active = dict(active_request or {})
    stage = str(hb.get('stage') or active.get('last_stage') or active.get('stage') or 'EXEC_WORKER_ENTER')
    where = str(hb.get('where') or active.get('last_where') or active.get('where') or '')
    text = str(reason or 'worker_timeout')
    if 'timeout_stage=' not in text:
        text += '|timeout_stage=' + stage[:180]
    if where and '|where=' not in text:
        text += '|where=' + where[:180]
    return text, stage, where
