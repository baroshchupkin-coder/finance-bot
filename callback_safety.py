from threading import Lock
from time import monotonic


class CallbackDebouncer:
    def __init__(self, window_seconds=15):
        self.window_seconds = float(window_seconds)
        self._claims = {}
        self._lock = Lock()

    def claim(self, action, request_id, now=None):
        current = monotonic() if now is None else float(now)
        key = (str(action), str(request_id))
        with self._lock:
            expired_before = current - self.window_seconds
            self._claims = {
                existing_key: claimed_at
                for existing_key, claimed_at in self._claims.items()
                if claimed_at > expired_before
            }
            if key in self._claims:
                return False
            self._claims[key] = current
            return True

    def release(self, action, request_id):
        key = (str(action), str(request_id))
        with self._lock:
            self._claims.pop(key, None)
