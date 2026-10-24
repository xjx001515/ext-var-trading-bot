"""A page cache scoped to its browser connection and owning Python thread."""
from __future__ import annotations

import threading
import weakref


class PageRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._pages = {}

    def _prune(self):
        for key, (browser_ref, page_ref) in list(self._pages.items()):
            if browser_ref() is None or page_ref() is None:
                self._pages.pop(key, None)
        while len(self._pages) > 64:
            self._pages.pop(next(iter(self._pages)))

    def get(self, browser, platform):
        key = (threading.get_ident(), id(browser), str(platform).upper())
        with self._lock:
            self._prune()
            item = self._pages.get(key)
            if item is None or item[0]() is not browser:
                return None
            return item[1]()

    def set(self, browser, platform, page):
        key = (threading.get_ident(), id(browser), str(platform).upper())
        try:
            value = (weakref.ref(browser), weakref.ref(page))
        except TypeError:
            # A test/facade without weakref support simply uses normal discovery.
            return
        with self._lock:
            self._prune()
            self._pages[key] = value

    def forget(self, browser, platform):
        key = (threading.get_ident(), id(browser), str(platform).upper())
        with self._lock:
            self._pages.pop(key, None)
