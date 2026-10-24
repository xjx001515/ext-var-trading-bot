import gc
import threading
import unittest
from bot_core.browser_registry import PageRegistry


class Object:
    pass


class BrowserRegistryTests(unittest.TestCase):
    def test_connection_and_platform_scoped(self):
        registry = PageRegistry()
        a, b, page = Object(), Object(), Object()
        registry.set(a, 'EXT', page)
        self.assertIs(registry.get(a, 'EXT'), page)
        self.assertIsNone(registry.get(b, 'EXT'))
        self.assertIsNone(registry.get(a, 'VAR'))

    def test_foreign_thread_never_gets_page_wrapper(self):
        registry = PageRegistry()
        browser, page = Object(), Object()
        registry.set(browser, 'EXT', page)
        results = []
        worker = threading.Thread(target=lambda: results.append(registry.get(browser, 'EXT')))
        worker.start()
        worker.join(2)
        self.assertEqual(results, [None])
        self.assertIs(registry.get(browser, 'EXT'), page)

    def test_cache_does_not_keep_closed_connection_alive(self):
        registry = PageRegistry()
        browser, page = Object(), Object()
        registry.set(browser, 'EXT', page)
        del page
        gc.collect()
        self.assertIsNone(registry.get(browser, 'EXT'))

    def test_forget_does_not_close_any_page(self):
        registry = PageRegistry()
        browser, page = Object(), Object()
        registry.set(browser, 'EXT', page)
        registry.forget(browser, 'EXT')
        self.assertIsNone(registry.get(browser, 'EXT'))


if __name__ == '__main__':
    unittest.main()
