"""Bounded Tk log updates and one-at-a-time panel collection. No trading IO."""
from collections import deque
from copy import deepcopy
import re
import threading


MAX_LINES = 8000
MAX_CHARS = 2_000_000


def log_tag(line):
    low, upper = line.lower(), line.upper()
    if any(x in low for x in ('error', 'exception', 'traceback')) or '错误' in line:
        return 'tag_red_bold'
    if 'warn' in low or '警告' in line:
        return 'tag_yellow_bold'
    if any(x in line for x in ('扫描开始', '扫描结束', '预计下次扫描', '持仓进度', '扫描进度')):
        return 'tag_blue_bold'
    if '[STANDBY]' in line or 'module_disabled' in line:
        return 'tag_gray'
    match = re.search(r'\baction=([A-Za-z0-9_+\-]+)', line)
    action = match[1].lower() if match else ''
    if (any(x in line for x in ('平仓', '减仓', '全平', '双平', '止盈', '减亏'))
            or re.search(r'/L\d+[平减]', line) or 'close' in action
            or action in ('reduce', 'decrease', 'take_profit', 'tp')):
        return 'tag_green_bold'
    if (any(x in line for x in ('开仓', '加仓', '补仓', '补腿', '回补', '首开'))
            or re.search(r'/L\d+[开加]', line) or 'open' in action
            or action in ('increase', 'add', 'add_layer', 'add_leg', 'fix_leg', 'first_open')):
        return 'tag_red_bold'
    if any(x in upper for x in ('M10', 'B2', 'M9', 'B1')):
        return 'tag_yellow_bold'
    return ''


def bounded_lines(text, max_lines=MAX_LINES, max_chars=MAX_CHARS):
    """Normalize the Tk trailing newline once; cap the display, never the log file."""
    rows = [line + '\n' for line in text.splitlines()]
    rows = rows[-max_lines:]
    result, remaining = [], max_chars
    for line in reversed(rows):
        if remaining <= 0:
            break
        result.append(line[-remaining:])
        remaining -= len(result[-1])
    return tuple(reversed(result))


def line_patch(old, new):
    """(front deletion, retained prefix, replaced count, insertions), in O(n)."""
    if old == new:
        return 0, len(old), 0, ()
    # KMP suffix/prefix overlap handles a moving tail window and duplicate lines.
    values = list(new) + [object()] + list(old)
    prefix = [0] * len(values)
    for i in range(1, len(values)):
        j = prefix[i - 1]
        while j and values[i] != values[j]:
            j = prefix[j - 1]
        if values[i] == values[j]:
            j += 1
        prefix[i] = j
    overlap = prefix[-1] if prefix else 0
    if overlap:
        return len(old) - overlap, overlap, 0, new[overlap:]
    same = 0
    while same < min(len(old), len(new)) and old[same] == new[same]:
        same += 1
    tail = 0
    while tail < min(len(old), len(new)) - same and old[-tail - 1] == new[-tail - 1]:
        tail += 1
    return 0, same, len(old) - same - tail, new[same:len(new) - tail if tail else len(new)]


class SnapshotWorker:
    """At most one daemon job/result. Worker callbacks must never access Tk."""
    def __init__(self):
        self._lock = threading.Lock()
        self._busy = False
        self._result = None
        self._closed = False

    def start(self, collect):
        with self._lock:
            if self._closed or self._busy or self._result is not None:
                return False
            self._busy = True
        def run():
            try:
                result = (deepcopy(collect()), None)
            except Exception as exc:
                result = (None, exc)
            with self._lock:
                self._busy = False
                if not self._closed:
                    self._result = result
        try:
            threading.Thread(target=run, name='panel-snapshot', daemon=True).start()
        except Exception:
            with self._lock:
                self._busy = False
            raise
        return True

    def take(self):
        with self._lock:
            result, self._result = self._result, None
            return result

    def close(self):
        with self._lock:
            self._closed = True
            self._result = None


class LogView:
    """Preserve the document while reading; coalesce arrivals and append in batches."""
    def __init__(self, text, formatter, status=lambda value: None, *, batch_chars=12000):
        self.text, self.formatter, self.status = text, formatter, status
        self.batch_chars = batch_chars
        self.following = True
        self._source = ()
        self._key = None
        self._shown_source = None
        self._applied = ()
        self._work = None
        self._job = None
        self._scroll_job = None
        self._mouse_down = False
        self._closed = False
        self._status = None
        text.configure(state='disabled', undo=False)
        for event in ('<MouseWheel>', '<Button-4>', '<Button-5>', '<Shift-MouseWheel>',
                      '<Key-Prior>', '<Key-Next>', '<Key-Up>', '<Key-Down>', '<Home>', '<End>', '<Control-Home>'):
            text.bind(event, self._user_scroll, add='+')
        text.bind('<Control-End>', lambda event: (self.follow_latest(), 'break')[1], add='+')
        text.bind('<ButtonPress-1>', self._press, add='+')
        text.bind('<B1-Motion>', self._user_scroll, add='+')
        text.bind('<ButtonRelease-1>', self._release, add='+')
        text.bind('<Destroy>', lambda event: self.close() if event.widget is text else None, add='+')
        self._show_status()

    def _show_status(self):
        value = '跟随最新' if self.following else '正在查看历史'
        if not self.following and self._source != self._shown_source:
            value += ' · 有新日志'
        if value != self._status:
            self._status = value
            self.status(value)

    def set_source(self, lines, key):
        source = tuple(lines or ())
        if source == self._source and key == self._key:
            return
        if key != self._key:
            # A user filter/source change intentionally requests a different view.
            self._cancel('_job')
            self._work = None
            self._applied = ()
            self.text.configure(state='normal')
            self.text.delete('1.0', 'end')
            self.text.configure(state='disabled')
            self.following = True
        self._source, self._key = source, key
        self._show_status()
        self._schedule()

    def _cancel(self, name):
        job = getattr(self, name)
        if job is not None:
            try:
                self.text.after_cancel(job)
            except Exception:
                pass
            setattr(self, name, None)

    def _schedule(self):
        if not self._closed and self.following and self._job is None:
            self._job = self.text.after(8, self._flush)

    def _flush(self):
        self._job = None
        if self._closed or not self.following or self._mouse_down:
            return
        if self._work is None:
            source = self._source
            rows = bounded_lines(self.formatter(source, self._key))
            drop, prefix, remove, insert = line_patch(self._applied, rows)
            if not (drop or remove or insert):
                self._shown_source = source
                self._show_status()
                return
            self.text.configure(state='normal')
            try:
                if drop:
                    self.text.delete('1.0', f'{drop + 1}.0')
                if remove:
                    self.text.delete(f'{prefix + 1}.0', f'{prefix + remove + 1}.0')
                self.text.mark_set('_panel_log_insert', f'{prefix + 1}.0')
                self.text.mark_gravity('_panel_log_insert', 'right')
            finally:
                self.text.configure(state='disabled')
            self._work = (source, rows, deque((line, log_tag(line)) for line in insert))
        source, rows, pending = self._work
        args, budget = [], self.batch_chars
        while pending and budget > 0:
            line, tag = pending.popleft()
            piece = line[:budget]
            if len(piece) < len(line):
                pending.appendleft((line[len(piece):], tag))
            args.extend((piece, (tag,) if tag else ()))
            budget -= len(piece)
        if args:
            self.text.configure(state='normal')
            try:
                self.text.insert('_panel_log_insert', *args)
            finally:
                self.text.configure(state='disabled')
        self.text.see('end')
        if not pending:
            self._applied, self._shown_source, self._work = rows, source, None
        if pending or source != self._source:
            self._schedule()
        self._show_status()

    def _user_scroll(self, event=None):
        self.following = False
        self._cancel('_job')
        if self._scroll_job is None:
            self._scroll_job = self.text.after_idle(self._sync_scroll)
        self._show_status()

    def _sync_scroll(self):
        self._scroll_job = None
        if self._closed:
            return
        if not self._mouse_down and not self.text.tag_ranges('sel') and float(self.text.yview()[1]) >= 0.999:
            self.follow_latest()
        else:
            self._show_status()

    def _press(self, event=None):
        self._mouse_down = True
        self._user_scroll(event)

    def _release(self, event=None):
        self._mouse_down = False
        self._user_scroll(event)

    def scroll(self, *args):
        self._user_scroll()
        self.text.yview(*args)

    def follow_latest(self):
        if self._closed:
            return
        self._cancel('_scroll_job')
        self._mouse_down = False
        self.following = True
        self.text.see('end')
        self._show_status()
        self._schedule()

    def close(self):
        self._closed = True
        self._cancel('_job')
        self._cancel('_scroll_job')
