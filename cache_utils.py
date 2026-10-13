import threading


_MISSING = object()


class BoundedCache(dict):
    """Thread-safe FIFO cache with a fixed maximum item count."""

    def __init__(self, max_items):
        super().__init__()
        self.max_items = max(
            1,
            int(max_items)
        )
        self._lock = threading.RLock()

    def __getitem__(self, key):
        with self._lock:
            return super().__getitem__(key)

    def __setitem__(self, key, value):
        with self._lock:
            if super().__contains__(key):
                super().__delitem__(key)

            super().__setitem__(
                key,
                value
            )

            while (
                super().__len__()
                > self.max_items
            ):
                oldest_key = next(
                    super().__iter__()
                )
                super().__delitem__(
                    oldest_key
                )

    def __contains__(self, key):
        with self._lock:
            return super().__contains__(key)

    def __len__(self):
        with self._lock:
            return super().__len__()

    def get(self, key, default=None):
        with self._lock:
            return super().get(
                key,
                default
            )

    def pop(self, key, default=_MISSING):
        with self._lock:
            if default is _MISSING:
                return super().pop(key)

            return super().pop(
                key,
                default
            )

    def clear(self):
        with self._lock:
            super().clear()
