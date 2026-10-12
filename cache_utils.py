import threading


class BoundedCache(dict):
    """작은 개인 서버에서 캐시가 무한정 커지지 않도록 개수를 제한한다."""

    def __init__(self, max_items):
        super().__init__()
        self.max_items = max_items
        self._lock = threading.RLock()

    def __setitem__(self, key, value):
        with self._lock:
            if key in self:
                super().__delitem__(key)
            super().__setitem__(key, value)
            while len(self) > self.max_items:
                oldest_key = next(iter(self))
                super().__delitem__(oldest_key)

    def get(self, key, default=None):
        with self._lock:
            return super().get(key, default)

    def clear(self):
        with self._lock:
            return super().clear()

