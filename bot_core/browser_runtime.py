"""Browser ownership and bounded platform recovery. No startup or trading IO."""
from __future__ import annotations

import inspect
import threading
import time
import weakref
from urllib.parse import urlsplit, urlunsplit


class SessionLiveness:
    """A scanner ending its driver cannot invalidate another thread's driver."""
    def __init__(self):
        self._lock = threading.RLock()
        self._owners = weakref.WeakKeyDictionary()

    def begin(self, browser=None):
        with self._lock:
            self._owners[threading.current_thread()] = {'ready': True, 'valid': True, 'browser': browser}

    def set_ready(self, ready):
        with self._lock:
            record = self._owners.get(threading.current_thread())
            if record is not None:
                record['ready'] = bool(ready)

    def end(self):
        with self._lock:
            self._owners.pop(threading.current_thread(), None)

    def invalidate(self, browser=None, *, all_sessions=False):
        with self._lock:
            for record in self._owners.values():
                if all_sessions or (browser is not None and record['browser'] is browser):
                    record['valid'] = False

    def current_alive(self):
        with self._lock:
            record = dict(self._owners.get(threading.current_thread()) or {})
        if not record.get('ready') or not record.get('valid'):
            return False
        browser = record.get('browser')
        if browser is None:  # Driver entered; CDP connection has not been made yet.
            return True
        try:
            return bool(browser.is_connected())
        except Exception:
            return False

    def any_ready(self):
        # Diagnostic summary only; never call a foreign thread's browser methods.
        with self._lock:
            return any(t.is_alive() and r['ready'] and r['valid'] for t, r in self._owners.items())


def supported_kwargs(fn, kwargs):
    """Inspect the original SDK signature, including methods wrapped by guards."""
    try:
        params = inspect.signature(fn).parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(kwargs)
        return {key: value for key, value in kwargs.items() if key in params}
    except (TypeError, ValueError):
        return dict(kwargs)


def safe_url(value):
    try:
        parsed = urlsplit(str(value or ''))
        return urlunsplit((parsed.scheme, parsed.hostname or '', parsed.path, '', ''))[:220]
    except Exception:
        return ''


class ExtRecovery:
    """Reuse one owned recovery target; alternate CDP navigation on that target."""
    def __init__(self, clock=time.time, *, platform='EXT', host=None, manual_url=None):
        self.clock = clock
        self.platform = str(platform).upper()
        self.host = host or ('omni.variational.io' if self.platform == 'VAR' else 'app.extended.exchange')
        self.manual_url = manual_url or (self.host + ('/perpetual/BTC' if self.platform == 'VAR' else '/trade'))
        self._lock = threading.RLock()
        self.first_failure = None
        self.last_error = ''
        self.failures = 0
        self.last_alarm = None
        self.candidate_id = ''
        self.candidate_ref = None
        self.creation_uncertain = False
        self._observed = weakref.WeakSet()

    def fail(self, reason):
        with self._lock:
            if self.first_failure is None:
                self.first_failure = self.clock()
            self.failures += 1
            self.last_error = str(reason).splitlines()[0][:240]

    def success(self):
        with self._lock:
            self.first_failure = None
            self.last_error = ''
            self.failures = 0
            self.last_alarm = None

    def status(self):
        with self._lock:
            elapsed = 0.0 if self.first_failure is None else max(0.0, self.clock() - self.first_failure)
            alarm = self.first_failure is not None and elapsed >= 600
            return {'active': self.first_failure is not None, 'alarm': alarm,
                    'since_ts': self.first_failure, 'elapsed_sec': int(elapsed),
                    'failures': self.failures, 'last_error': self.last_error,
                    'candidate_target_id': self.candidate_id,
                    'message': (f'{self.platform} 页面恢复失败已持续 {int(elapsed // 60)} 分钟，交易执行受阻；'
                                f'请手动打开 {self.manual_url}') if alarm else ''}

    def alarm_due(self):
        with self._lock:
            if not self.status()['alarm']:
                return False
            if self.last_alarm is not None and self.clock() - self.last_alarm < 300:
                return False
            self.last_alarm = self.clock()
            return True

    @staticmethod
    def _target_id(page, call):
        session = call('ext_recovery.target_session', page.context.new_cdp_session, page)
        try:
            result = call('ext_recovery.target_info', session.send, 'Target.getTargetInfo')
            return str((result.get('targetInfo') or {}).get('targetId') or '')
        finally:
            session.detach()

    def observe(self, page, call, log):
        if page in self._observed:
            return
        self._observed.add(page)
        target = ''
        try:
            target = self._target_id(page, call)
        except Exception:
            pass
        def emit(event, **detail):
            try:
                log(event, platform=self.platform, target_id=target, page_id=hex(id(page)), thread_id=threading.get_ident(), **detail)
            except Exception:
                pass  # Diagnostic callbacks must not break the browser event loop.
        def navigation_request(event, request, **detail):
            try:
                if request.is_navigation_request() and request.frame == page.main_frame:
                    emit(event, url=safe_url(request.url), **detail)
            except Exception:
                pass
        try:
            page.on('close', lambda *a: emit('PAGE_CLOSED'))
            page.on('crash', lambda *a: emit('PAGE_CRASHED'))
            page.on('framenavigated', lambda frame: emit('NAV_COMMITTED', url=safe_url(frame.url)) if frame == page.main_frame else None)
            page.on('request', lambda req: navigation_request('NAV_REQUEST_BEGIN', req))
            page.on('response', lambda response: navigation_request('NAV_RESPONSE', response.request, status=response.status))
            page.on('requestfailed', lambda req: emit('NAV_REQUEST_FAILED', url=safe_url(req.url),
                    failure=str(req.failure or '')[:160]) if req.is_navigation_request() else None)
            emit('PAGE_OBSERVED', url=safe_url(page.url))
        except Exception:
            pass

    def _candidate(self, browser, call):
        pages = [p for c in browser.contexts for p in c.pages if not p.is_closed()]
        old = self.candidate_ref() if self.candidate_ref else None
        if old is not None and any(p is old for p in pages):
            self._check_candidate_platform(old)
            return old
        if self.candidate_id:
            for page in pages:
                try:
                    matches = self._target_id(page, call) == self.candidate_id
                except Exception:
                    continue
                if matches:
                    self._check_candidate_platform(page)
                    return page
            # Before another creation, prove the previous target no longer exists.
            session = call('ext_recovery.browser_session', browser.new_browser_cdp_session)
            try:
                result = call('ext_recovery.targets', session.send, 'Target.getTargets')
                if any(t.get('targetId') == self.candidate_id for t in result.get('targetInfos', [])):
                    raise RuntimeError('owned_target_exists_but_not_attached')
            finally:
                session.detach()
            self.candidate_id = ''
            self.candidate_ref = None
            self.creation_uncertain = False
        elif self.creation_uncertain or self.candidate_ref is not None:
            raise RuntimeError('recovery_target_identity_unknown_no_duplicate_creation')
        return None

    def _check_candidate_platform(self, page):
        host = urlsplit(str(page.url or '')).hostname
        if host in ('app.extended.exchange', 'omni.variational.io') and host != self.host:
            raise RuntimeError('recovery_target_is_other_platform_no_navigation')

    def recover(self, browser, url, *, page=None, call, log, timeout_ms=26000):
        original_log = log
        def log(event, **detail):
            original_log(event, **dict(detail, platform=self.platform))
        with self._lock:
            if self.first_failure is None:
                self.first_failure = self.clock()
                self.last_error = 'recovery_in_progress'
        created_with_url = False
        if page is None or page.is_closed():
            page = self._candidate(browser, call)
        if page is not None:
            self._check_candidate_platform(page)
        if page is None:
            if not browser.contexts:
                raise RuntimeError('existing_browser_context_missing')
            self.creation_uncertain = True
            if self.failures:
                # Reached only after proving the previous target absent. Listen
                # before creation so a fast target attachment cannot be missed.
                session = call('ext_recovery.browser_session', browser.new_browser_cdp_session)
                try:
                    with browser.contexts[0].expect_page(timeout=5000) as waiting:
                        result = call('ext_recovery.create_target', session.send, 'Target.createTarget',
                                      {'url': url, 'background': False}, _operation_timeout_ms=timeout_ms)
                        self.candidate_id = str(result.get('targetId') or '')
                    page = waiting.value
                    created_with_url = True
                    log('TARGET_CREATED_WITH_URL', target_id=self.candidate_id, url=safe_url(url))
                finally:
                    session.detach()
            else:
                page = call('ext_recovery.new_page', browser.contexts[0].new_page)
            self.candidate_ref = weakref.ref(page)
            try:
                self.candidate_id = self._target_id(page, call)
                self.creation_uncertain = not bool(self.candidate_id)
            except Exception:
                pass  # Retain the known page object, never create an unknown duplicate.
        # Also retain original tabs, not only tabs created by recovery. A new CDP
        # wrapper must rediscover this target rather than create a duplicate.
        self.candidate_ref = weakref.ref(page)
        try:
            self.candidate_id = self._target_id(page, call)
            self.creation_uncertain = not bool(self.candidate_id)
        except Exception:
            self.creation_uncertain = True
        self.observe(page, call, log)
        call('ext_recovery.front', page.bring_to_front)
        try:
            log('NAV_BEGIN', url=safe_url(url), target_id=self.candidate_id, mode='page_goto_commit')
            if not created_with_url:
                call('ext_recovery.goto', page.goto, url, wait_until='commit', timeout=timeout_ms)
        except Exception as exc:
            log('NAV_COMMIT_FAILED', error=type(exc).__name__, url=safe_url(page.url))
            # The original target survives. Navigate it through CDP instead of
            # opening a second tab that could later duplicate a delayed navigation.
            session = call('ext_recovery.cdp_session', page.context.new_cdp_session, page)
            try:
                call('ext_recovery.cdp_stop_loading', session.send, 'Page.stopLoading', _operation_timeout_ms=timeout_ms)
                result = call('ext_recovery.cdp_navigate', session.send, 'Page.navigate', {'url': url}, _operation_timeout_ms=timeout_ms)
                if result.get('errorText'):
                    raise RuntimeError('cdp_navigation_failed:' + str(result['errorText'])[:160])
                log('CDP_NAV_ACCEPTED', target_id=self.candidate_id, frame_id=result.get('frameId', ''))
            finally:
                session.detach()
        call('ext_recovery.document_ready', page.wait_for_load_state, 'domcontentloaded', timeout=timeout_ms)
        call('ext_recovery.body_ready', page.wait_for_function,
             "host => location.hostname === host && !!document.body && document.body.innerText.trim().length > 3",
             arg=self.host,
             timeout=4500)
        return page


class PageIdentity:
    """Stable target IDs across CDP wrappers; weak, unique fallback if unavailable."""
    def __init__(self):
        self._lock = threading.RLock()
        self._pages = weakref.WeakKeyDictionary()
        self._serial = 0

    def key(self, page, platform, call):
        platform = str(platform).upper()
        with self._lock:
            cached = self._pages.get(page) or {}
            if platform in cached:
                return cached[platform]
        target = ''
        try:
            target = ExtRecovery._target_id(page, call)
        except Exception:
            pass
        with self._lock:
            self._serial += 1
            value = platform + ':' + (target or f'unverified:{self._serial}')
            current = dict(self._pages.get(page) or {})
            current[platform] = value
            self._pages[page] = current
            return value
