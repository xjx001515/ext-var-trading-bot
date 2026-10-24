"""Standalone withdrawn Tk widgets, fake collectors, temporary files; no live panel."""
import ast
import copy
import random
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import tkinter as tk

from ast_support import ROOT, load_definitions
from bot_core.panel_log import LogView, SnapshotWorker, bounded_lines, line_patch, log_tag
from bot_core import panel_log


class LinePlanTests(unittest.TestCase):
    def test_same_content_has_no_edits(self):
        rows=('a\n','b\n')
        self.assertEqual(line_patch(rows,rows),(0,2,0,()))

    def test_rolling_window_only_drops_expired_prefix_and_appends(self):
        self.assertEqual(line_patch(('a\n','b\n','c\n'),('b\n','c\n','d\n')),(1,2,0,('d\n',)))

    def test_middle_change_keeps_unchanged_ends(self):
        self.assertEqual(line_patch(('a\n','old\n','z\n'),('a\n','new\n','z\n')),(0,1,1,('new\n',)))

    def test_repeated_and_empty_lines_never_duplicate_or_drop_content(self):
        rng=random.Random(42)
        for _ in range(1000):
            old=tuple(rng.choice(('a\n','b\n','\n')) for _ in range(rng.randrange(20)))
            new=tuple(rng.choice(('a\n','b\n','\n')) for _ in range(rng.randrange(20)))
            drop,prefix,remove,insert=line_patch(old,new)
            result=list(old[drop:]);result[prefix:prefix+remove]=insert
            self.assertEqual(tuple(result),new)

    def test_view_is_bounded_and_trailing_newline_is_stable(self):
        self.assertEqual(bounded_lines('a\nb'),('a\n','b\n'))
        self.assertEqual(bounded_lines('a\nb\n'),('a\n','b\n'))
        self.assertEqual(bounded_lines('a\nb\nc',max_lines=2),('b\n','c\n'))
        self.assertEqual(sum(map(len,bounded_lines('x'*1000,max_chars=100))),100)
        self.assertEqual(bounded_lines('a\r\nb\rc\n\n'),('a\n','b\n','c\n','\n'))

    def test_existing_semantic_colors(self):
        for line,tag in [('error: failed','tag_red_bold'),('WARN warning','tag_yellow_bold'),
                         ('扫描开始','tag_blue_bold'),('[STANDBY]','tag_gray'),
                         ('module=M9 action=decrease','tag_green_bold'),('B1 开仓','tag_red_bold'),
                         ('M10 pending','tag_yellow_bold'),('普通内容','')]:
            self.assertEqual(log_tag(line),tag)


class CountingText(tk.Text):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.edits=[]
        self.full_reads=0
    def insert(self,*args):
        self.edits.append(('insert',args))
        return super().insert(*args)
    def delete(self,*args):
        self.edits.append(('delete',args))
        return super().delete(*args)
    def get(self,*args):
        self.full_reads+=1
        return super().get(*args)


class LogWidgetTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        self.text=CountingText(self.root,wrap='char',font=('Consolas',9))
        self.text.place(x=0,y=0,width=650,height=420)
        self.root.update_idletasks()
        self.status=[]
        self.formatter=Mock(side_effect=lambda lines,key:('\n\n' if key[0]=='human' else '\n').join(lines))
        self.view=LogView(self.text,self.formatter,self.status.append,batch_chars=1000)
        self.addCleanup(self.root.destroy)

    def flush(self):
        for _ in range(1000):
            self.root.update()
            if self.view._job is None:
                return
            time.sleep(.001)
        self.fail('log rendering did not finish within a bounded number of turns')

    def source(self,lines,key=('all','','fake.log')):
        self.view.set_source(lines,key);self.flush()

    def content(self):
        return tk.Text.get(self.text,'1.0','end-1c')

    def pause(self):
        self.text.yview_moveto(0)
        self.view._user_scroll();self.root.update()
        self.assertFalse(self.view.following)

    def test_unchanged_refresh_does_not_read_or_rebuild_widget(self):
        rows=[f'日志 {i}' for i in range(200)]
        self.source(rows);self.text.edits.clear();reads=self.text.full_reads
        self.source(list(rows))
        self.assertEqual(self.text.edits,[])
        self.assertEqual(self.text.full_reads,reads)
        self.assertEqual(self.formatter.call_count,1)

    def test_append_retains_existing_text_and_color(self):
        self.source(['原有日志','B1 开仓'])
        self.text.edits.clear()
        self.source(['原有日志','B1 开仓','M9 减仓'])
        self.assertFalse(any(kind=='delete' for kind,_ in self.text.edits))
        self.assertEqual(self.content(),'原有日志\nB1 开仓\nM9 减仓\n')
        self.assertIn('tag_red_bold',self.text.tag_names('2.0'))
        self.assertIn('tag_green_bold',self.text.tag_names('3.0'))

    def test_reading_freezes_content_selection_and_position_across_updates(self):
        rows=[f'历史行 {i} '+('内容 '*20) for i in range(300)]
        self.source(rows);self.pause()
        self.text.tag_add('sel','5.0','5.4')
        before=(self.content(),self.text.index('@0,0'),tuple(map(str,self.text.tag_ranges('sel'))))
        self.text.edits.clear()
        for i in range(20):self.source(rows[i+1:]+[f'新增{i}'])
        after=(self.content(),self.text.index('@0,0'),tuple(map(str,self.text.tag_ranges('sel'))))
        self.assertEqual(before,after);self.assertEqual(self.text.edits,[])
        self.assertIn('有新日志',self.status[-1])
        self.assertEqual(self.formatter.call_count,1,'history mode must not reformat on each refresh')

    def test_latest_button_applies_only_newest_buffered_snapshot(self):
        rows=[f'日志 {i}' for i in range(300)]
        self.source(rows);self.pause()
        self.source(['过时版本']);self.source(['最新版本','最终行'])
        self.view.follow_latest();self.flush()
        self.assertEqual(self.content(),'最新版本\n最终行\n')
        self.assertTrue(self.view.following);self.assertEqual(self.status[-1],'跟随最新')

    def test_scrolling_to_bottom_restores_following(self):
        rows=[f'日志 {i}' for i in range(300)]
        self.source(rows);self.pause();self.source(rows+['新行'])
        self.view.scroll('moveto',1.0);self.flush()
        self.assertTrue(self.view.following)
        self.assertTrue(self.content().endswith('新行\n'))

    def test_many_scroll_events_create_only_one_idle_callback(self):
        self.source([f'日志 {i}' for i in range(300)])
        self.text.yview_moveto(0)
        for _ in range(100):self.view._user_scroll()
        pending=self.root.tk.call('after','info')
        self.assertEqual(tuple(pending),(self.view._scroll_job,))
        self.root.update();self.assertFalse(self.view.following)

    def test_selection_at_bottom_prevents_new_text_from_disrupting_copy(self):
        self.source(['a','b']);self.text.tag_add('sel','1.0','1.1')
        self.view._press();self.view._release();self.root.update()
        self.assertFalse(self.view.following)
        self.source(['a','b','c']);self.assertEqual(self.content(),'a\nb\n')

    def test_filter_change_is_immediate_even_while_history_is_paused(self):
        self.source([f'日志 {i}' for i in range(300)]);self.pause()
        self.source(['匹配行'],('errors','匹配','fake.log'))
        self.assertEqual(self.content(),'匹配行\n');self.assertTrue(self.view.following)

    def test_human_blocks_keep_spacing_and_internal_line_colors(self):
        self.source(['标题\nB1 开仓','M9 减仓'],('human','','fake.log'))
        self.assertEqual(self.content(),'标题\nB1 开仓\n\nM9 减仓\n')
        self.assertIn('tag_green_bold',self.text.tag_names('4.0'))

    def test_rotating_or_truncated_file_does_not_leave_old_lines(self):
        self.source(['old1','old2','old3'])
        self.source(['new1']);self.assertEqual(self.content(),'new1\n')
        self.source([]);self.assertEqual(self.content(),'')

    def test_rolling_window_and_non_bmp_unicode_preserve_exact_content(self):
        self.source(['旧行','🚀 新行','后续'])
        self.source(['🚀 新行','后续','追加'])
        self.assertEqual(self.content(),'🚀 新行\n后续\n追加\n')

    def test_large_update_yields_between_bounded_tk_insertions(self):
        observed=[]
        self.view.set_source(['x'*10000],('all','','fake.log'))
        self.root.after(9,lambda:observed.append(len(self.content())))
        self.flush()
        self.assertTrue(observed and 0<observed[0]<10001)
        calls=[args for kind,args in self.text.edits if kind=='insert']
        self.assertGreater(len(calls),1)
        self.assertTrue(all(sum(len(s) for s in args[1::2])<=1000 for args in calls))
        self.assertEqual(self.content(),'x'*10000+'\n')

    def test_widget_destroy_cancels_pending_jobs(self):
        self.view.set_source(['x'*10000],('all','','fake.log'))
        self.text.destroy();self.root.update()
        self.assertTrue(self.view._closed)
        self.assertIsNone(self.view._job)


class CollectionTests(unittest.TestCase):
    def wait_result(self,worker):
        limit=time.monotonic()+2
        while time.monotonic()<limit:
            result=worker.take()
            if result is not None:return result
            time.sleep(.001)
        self.fail('fake collection did not complete')

    def test_single_flight_never_queues_duplicate_collectors(self):
        worker=SnapshotWorker();started=threading.Event();release=threading.Event()
        self.addCleanup(release.set);self.addCleanup(worker.close)
        calls=[]
        def collect():
            calls.append(threading.get_ident());started.set();release.wait(2);return {'ok':True}
        self.assertTrue(worker.start(collect));self.assertTrue(started.wait(1))
        for _ in range(100):self.assertFalse(worker.start(collect))
        self.assertNotEqual(calls[0],threading.get_ident())
        release.set();result,error=self.wait_result(worker)
        self.assertIsNone(error);self.assertEqual(result,{'ok':True});self.assertEqual(len(calls),1)

    def test_errors_are_returned_and_worker_can_be_used_again(self):
        worker=SnapshotWorker();self.addCleanup(worker.close)
        def fail():raise ValueError('fixture')
        worker.start(fail);_,error=self.wait_result(worker);self.assertIsInstance(error,ValueError)
        worker.start(lambda:123);self.assertEqual(self.wait_result(worker),(123,None))

    def test_close_discards_late_result_without_waiting_or_calling_ui(self):
        worker=SnapshotWorker();release=threading.Event();finished=threading.Event()
        self.addCleanup(release.set)
        def collect():release.wait(2);finished.set();return 123
        worker.start(collect);worker.close();release.set();self.assertTrue(finished.wait(1))
        self.assertFalse(worker.start(lambda:1));self.assertIsNone(worker.take())

    def test_actual_panel_poll_remains_responsive_during_slow_collection(self):
        env={'_panel_log':panel_log,'copy':copy,'threading':threading,'time':time,'REFRESH_MS':1500,'hashlib':__import__('hashlib'),
             'messagebox':SimpleNamespace(showerror=Mock())}
        load_definitions({'PanelUI'},env,path=ROOT/'面板4-11.py')
        panel=object.__new__(env['PanelUI']);worker=SnapshotWorker();self.addCleanup(worker.close)
        started=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def collect():started.set();release.wait(2);return {'snapshot':1}
        panel.model=SimpleNamespace(refresh=collect,cfg=SimpleNamespace(refresh_ms=1500))
        panel.root=SimpleNamespace(after=Mock(return_value='poll'))
        panel._refresh_worker=worker;panel._refresh_model_lock=threading.RLock()
        panel._refresh_generation=0;panel._next_refresh_at=0;panel._render=Mock();panel.var_status=Mock()
        begin=time.perf_counter();panel._refresh_loop();elapsed=time.perf_counter()-begin
        self.assertLess(elapsed,.1);self.assertTrue(started.wait(1));panel._render.assert_not_called()
        panel._refresh_loop();panel._render.assert_not_called()
        release.set()
        deadline=time.monotonic()+2
        while not panel._render.called and time.monotonic()<deadline:
            panel._refresh_loop();time.sleep(.001)
        panel._render.assert_called_once_with({'snapshot':1})

    def test_actual_panel_discards_snapshot_from_previous_directory(self):
        env={'time':time,'REFRESH_MS':1500,'hashlib':__import__('hashlib'),'messagebox':Mock()}
        load_definitions({'PanelUI'},env,path=ROOT/'面板4-11.py')
        panel=object.__new__(env['PanelUI'])
        panel._refresh_worker=SimpleNamespace(take=lambda:((0,{'stale':True}),None),start=Mock())
        panel._refresh_generation=1;panel._next_refresh_at=0
        panel.model=SimpleNamespace(cfg=SimpleNamespace(refresh_ms=1500));panel._render=Mock()
        panel.root=SimpleNamespace(after=Mock());panel.var_status=Mock()
        panel._refresh_loop();panel._render.assert_not_called();panel._refresh_worker.start.assert_called_once()

    def test_hidden_tabs_do_not_read_or_render(self):
        env={};load_definitions({'PanelUI'},env,path=ROOT/'面板4-11.py')
        panel=object.__new__(env['PanelUI']);panel._latest_panel_data={'panel_status':{'x':1}}
        for name in ('grid','grid_book','trend_observe','selection_observe'):
            setattr(panel,'tab_'+name,name);setattr(panel,'_render_'+name+'_tab',Mock())
        panel.nb_right=SimpleNamespace(select=lambda:'overview')
        panel._render_visible_tab()
        for name in ('grid','grid_book','trend_observe','selection_observe'):
            getattr(panel,'_render_'+name+'_tab').assert_not_called()
        panel.nb_right.select=lambda:'trend_observe';panel._render_visible_tab()
        panel._render_trend_observe_tab.assert_called_once_with({'x':1})

    def test_tail_cache_reads_file_once_and_invalidates_for_new_limits(self):
        env={'_tail_lines':Mock(return_value=['a','b','c','d']), '_slice_recent_rounds':lambda r:r}
        load_definitions({'PanelModel'},env,path=ROOT/'面板4-11.py')
        model=object.__new__(env['PanelModel']);model._full_log_cache={}
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'fixture.log';p.write_text('fixture')
            self.assertEqual(model._get_full_log_tails_cached(p,2,4),(['c','d'],['a','b','c','d']))
            model._get_full_log_tails_cached(p,2,4);env['_tail_lines'].assert_called_once()
            model._get_full_log_tails_cached(p,1,3);self.assertEqual(env['_tail_lines'].call_count,2)


if __name__=='__main__':
    unittest.main()
