import copy
import csv
from datetime import timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from ast_support import ROOT, load_definitions
from bot_core.account_journal import (Journal, bot_event, event, number, observe_bot_event,
                                      reconciliation, redact, snapshot_events, timestamp)
from bot_core.exchange_history import (ExtendedReader, ReadError, _NoRedirect,
                                       import_var_csv, normalize_ext, normalize_var)
from maintenance.account_audit import LocalCollector


def trade(identity='fill1', **extra):
    return dict(id=identity, accountId=7, orderId='order1', market='Q-USD', side='BUY',
                qty='2', price='3.125', value='6.25', fee='0.0125',
                createdTime=1792716600000, tradeType='TRADE', **extra)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / 'account_audit')

    def tearDown(self):
        self.temp.cleanup()

    def test_real_fills_deduplicate_across_restart_but_partial_fills_do_not_merge(self):
        for row in (trade('f1'), trade('f2')):
            for record in normalize_ext('trades', row, '7'):
                self.assertTrue(self.journal.append(record))
        reopened = Journal(self.journal.directory)
        for record in normalize_ext('trades', trade('f1'), '7'):
            self.assertFalse(reopened.append(record))
        fills = [r for r in reopened.records(latest=True) if r['kind'] == 'fill']
        self.assertEqual(len(fills), 2)
        self.assertEqual(sum(Decimal(r['data']['quantity']) for r in fills), 4)

    def test_account_and_platform_are_part_of_identity(self):
        for account in ('7', '8'):
            row = trade(); row['accountId'] = int(account)
            self.journal.append(normalize_ext('trades', row, account)[0])
        self.assertEqual(len(list(self.journal.records(latest=True))), 2)

    def test_original_versions_are_immutable(self):
        self.journal.append(normalize_ext('trades', trade(), '7')[0])
        for sql in ('DELETE FROM events', "UPDATE events SET payload='{}'"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.journal.connect() as db:
                    db.execute(sql)
        self.assertEqual(len(list(self.journal.records())), 1)

    def test_old_order_update_cannot_replace_newer_exchange_status(self):
        latest = dict(id='o', status='FILLED', updatedTime=1792716600000)
        older = dict(id='o', status='CANCELLED', updatedTime=1792716500000)
        for row in (latest, older):
            self.journal.append(normalize_ext('orders', row, '7')[0])
        self.assertEqual(len(list(self.journal.records())), 2)
        self.assertEqual(list(self.journal.records(latest=True))[0]['data']['status'], 'filled')

    def test_export_is_rebuildable_without_erasing_journal(self):
        self.journal.append(normalize_ext('trades', trade(), '7')[0])
        self.journal.export()
        output = self.journal.directory / 'events.jsonl'
        output.write_text('broken export')
        self.assertEqual(self.journal.export(), 1)
        self.assertEqual(json.loads(output.read_text())['source_id'], 'fill1')
        self.assertEqual(len(list(self.journal.records())), 1)

    def test_bot_success_is_never_promoted_to_exchange_fill(self):
        value = {'ts': 1792716600, 'ok': True, 'fill_confirmed': True,
                 'filled_qty': 2, 'req': {'platform': 'EXT', 'coin': 'Q', 'req_id': 'r1'}}
        self.journal.append(bot_event(value, 'executor_results.jsonl'))
        summary = self.journal.summary()
        self.assertFalse(any(r['authority'] == 'exchange_record' for r in self.journal.records()))
        self.assertIsNone(summary['verified_net_profit'])
        self.assertEqual(summary['fill_links'], [])

    def test_only_exact_unique_order_id_links_execution_to_fill(self):
        self.journal.append(bot_event({'ts': 1, 'order_id': 'order1', 'req_id': 'r1',
                                      'platform': 'EXT'}, 'executor_results.jsonl'))
        self.journal.append(normalize_ext('trades', trade(), '7')[0])
        self.assertEqual(self.journal.summary()['fill_links'][0]['req_id'], 'r1')
        self.journal.append(bot_event({'ts': 2, 'order_id': 'order1', 'req_id': 'r2',
                                      'platform': 'EXT'}, 'executor_results.jsonl'))
        self.assertIsNone(self.journal.summary()['fill_links'][0]['req_id'])

    def test_fees_are_actual_costs_and_missing_fees_stay_unknown(self):
        for identity, fee in (('known', '0.0125'), ('unknown', None)):
            row = trade(identity); row['fee'] = fee
            for record in normalize_ext('trades', row, '7'):
                self.journal.append(record)
        fee = self.journal.summary()['cash_components_observed_only'][0]
        self.assertEqual(Decimal(fee['known_cash_delta']), Decimal('-0.0125'))
        self.assertEqual(fee['unknown_count'], 1)

    def test_order_identity_collision_across_accounts_remains_unlinked(self):
        self.journal.append(bot_event({'ts': 1, 'order_id': 'order1', 'req_id': 'r1',
                                      'platform': 'EXT'}, 'executor_results.jsonl'))
        for account in (7, 8):
            row = trade(); row['accountId'] = account
            self.journal.append(normalize_ext('trades', row, str(account))[0])
        self.assertTrue(all(link['req_id'] is None for link in self.journal.summary()['fill_links']))

    def test_concurrent_duplicate_writers_keep_one_exchange_fill(self):
        from concurrent.futures import ThreadPoolExecutor
        record = normalize_ext('trades', trade(), '7')[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.journal.append(record), range(12)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(list(self.journal.records())), 1)

    def test_secret_fields_are_redacted_recursively(self):
        record = event('EXT','7','note','x',source='test',authority='collection_status',occurred_at=None,
            data={'api_key':'secret','nested':{'Authorization':'bearer','privateKey':'private'}},raw=None)
        self.journal.append(record)
        self.journal.export()
        text = (self.journal.directory/'events.jsonl').read_text()
        self.assertNotIn('bearer', text)
        self.assertNotIn(':"secret"', text)
        self.assertIn('[REDACTED]', text)

    def test_bot_hooks_do_not_mutate_input_or_other_ledgers(self):
        (self.root/'out').mkdir()
        sentinel = self.root/'out/trades.jsonl'; sentinel.write_text('unchanged')
        value={'ts':1,'ok':True,'req':{'platform':'EXT','params':{'amount_usd':160}}}
        before=copy.deepcopy(value)
        observe_bot_event(self.root/'bot.py', 'executor_results.jsonl', value)
        self.assertEqual(value,before)
        self.assertEqual(sentinel.read_text(),'unchanged')

    def test_collector_survives_truncation_and_ignores_partial_json(self):
        (self.root/'out').mkdir()
        path=self.root/'out/executor_results.jsonl'
        first={'ts':1,'req_id':'first','platform':'EXT','ok':False}
        path.write_text(json.dumps(first)+'\n'+ '{"ts":',encoding='utf-8')
        collector=LocalCollector(self.root,self.journal)
        self.assertEqual(collector.collect(),1)
        path.write_text(json.dumps({'ts':2,'req_id':'second','platform':'EXT'})+'\n',encoding='utf-8')
        self.assertEqual(collector.collect(),2)  # New record plus source-reset evidence.
        self.assertEqual(len([r for r in self.journal.records() if r['kind']=='bot_execution']),2)
        self.assertTrue(any(r['kind']=='source_reset' for r in self.journal.records()))
        self.assertEqual(collector.collect(),0)

    def test_collector_parse_error_is_visible_without_new_valid_trades(self):
        (self.root/'out').mkdir()
        (self.root/'out/executor_results.jsonl').write_text('{bad json}\n',encoding='utf-8')
        collector = LocalCollector(self.root,self.journal)
        self.assertEqual(collector.collect(),1)
        self.assertEqual(list(self.journal.records())[0]['kind'],'parse_error')

    def test_watcher_recovers_after_storage_failure(self):
        from maintenance.account_audit import watch
        fake = types.SimpleNamespace(collect=unittest.mock.Mock(side_effect=[OSError('disk full'), 0]))
        with patch('maintenance.account_audit.LocalCollector',return_value=fake), \
             patch('maintenance.account_audit.time.monotonic',side_effect=[0,0.1,0.3]), \
             patch('maintenance.account_audit.time.sleep'):
            watch(self.root,self.journal,5,duration=0.2)
        status = json.loads((self.journal.directory/'collector_status.json').read_text())
        self.assertEqual(fake.collect.call_count,2)
        self.assertEqual(status['state'],'collecting')
        self.assertFalse(status['trading_actions'])

    def test_snapshot_missing_balance_is_not_zero(self):
        rows=snapshot_events({'ts':1,'balances':{'EXT':{'usd':0}},'data_quality':'fail'})
        self.assertEqual(rows[0]['data']['equity'],'0')
        self.assertIsNone(rows[1]['data']['equity'])


class AdapterTests(unittest.TestCase):
    def test_both_documented_ext_fill_schemas(self):
        row=trade(); row['averagePrice']=row.pop('price'); row['filledQty']=row.pop('qty')
        record=normalize_ext('trades',row,'7')[0]
        self.assertEqual(record['data']['quantity'],'2')
        self.assertEqual(record['data']['price'],'3.125')

    def test_missing_price_or_qty_does_not_confirm_fill(self):
        for key in ('qty','price'):
            row=trade(); row.pop(key)
            self.assertEqual(normalize_ext('trades',row,'7')[0]['data']['status'],'unverified')

    def test_account_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):normalize_ext('trades',trade(),'999')

    def test_funding_size_is_not_assumed_paid_or_received(self):
        row={'id':1,'fundingFee':'1.5','fundingRate':'0.0001','paidTime':1792716600000}
        record=normalize_ext('funding',row,'7')[0]
        self.assertEqual(record['data']['amount_reported'],'1.5')
        self.assertIsNone(record['data']['cash_delta'])

    def test_asset_mapping_and_pending_transfer(self):
        row={'id':1,'type':'WITHDRAWAL','status':'IN_PROGRESS','asset':1,'amount':'4','time':1792716600000}
        record=normalize_ext('transfers',row,'7',asset_names={'1':'USDC'})[0]
        self.assertEqual(record['data']['cash_delta'],'-4')
        self.assertEqual(record['data']['status'],'pending')
        self.assertIsNone(normalize_ext('transfers',row,'7')[0]['data']['currency'])

    def test_position_pnl_is_reference_not_an_extra_cash_credit(self):
        row={'id':1,'realisedPnl':'12','closedTime':1792716600000}
        record=normalize_ext('positions',row,'7')[0]
        self.assertEqual(record['kind'],'position_summary')
        self.assertIsNone(record['data']['cash_delta'])

    def test_var_liquidation_and_failed_trade(self):
        row={'id':'v1','created_at':'2026-10-22T20:11:32+08:00','status':'confirmed',
             'underlying':'Q','price':'1.5','qty':'3','side':'sell','trade_type':'liquidation'}
        record=normalize_var(row,'main',channel='trades')
        self.assertTrue(record['data']['liquidation'])
        self.assertEqual(record['occurred_at'],'2026-10-22T12:11:32.000+00:00')
        row['status']='failed'
        self.assertEqual(normalize_var(row,'main',channel='trades')['data']['status'],'failed')

    def test_naive_timestamp_is_unknown_without_explicit_timezone(self):
        self.assertIsNone(timestamp('2026-10-22 20:11:32'))
        self.assertIsNotNone(timestamp('2026-10-22 20:11:32',naive_timezone=timezone.utc))

    def test_csv_funding_sign_requires_explicit_verification(self):
        row={'id':'x','created_at':'2026-10-22T00:00:00Z','status':'confirmed',
             'qty':'-2','asset':'USDC','transfer_type':'funding'}
        self.assertIsNone(normalize_var(row,'main',channel='transfers')['data']['cash_delta'])
        self.assertEqual(normalize_var(row,'main',channel='transfers',quantity_convention='signed')['data']['cash_delta'],'-2')

    def test_csv_reimport_deduplicates_and_never_claims_full_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'trades.csv'
            path.write_text('id,created_at,status,price,qty,underlying\na,2026-10-22T00:00:00Z,confirmed,1,2,Q\n',encoding='utf-8-sig')
            journal=Journal(Path(directory)/'journal')
            first=import_var_csv(journal,path,account='main',channel='trades')
            second=import_var_csv(journal,path,account='main',channel='trades')
            self.assertEqual(first['added'],1)
            self.assertEqual(second['added'],0)
            self.assertFalse(first['complete'])
            self.assertEqual(len([r for r in journal.records(latest=True) if r['kind']=='fill']),1)

    def test_nan_and_boolean_are_unknown_not_money(self):
        for value in (True,'NaN','Infinity','bad',None):self.assertIsNone(number(value))
        self.assertEqual(number('0.000000000000000001'),'0.000000000000000001')


class ApiTests(unittest.TestCase):
    def transport(self, mode):
        calls=[]
        def get(channel,params):
            calls.append((channel,dict(params)))
            if channel=='account':return {'status':'OK','data':{'accountId':7}}
            if channel=='assets':return {'status':'OK','data':[{'id':1,'name':'USDC'}]}
            if channel=='balance':return {'status':'OK','data':{'equity':'67.02'}}
            if channel!='trades':return {'status':'OK','data':[]}
            if 'cursor' not in params:return {'status':'OK','data':[trade()], 'pagination':{'cursor':55}}
            if mode=='error':raise ReadError('http_429')
            if mode=='loop':return {'status':'OK','data':[trade()], 'pagination':{'cursor':55}}
            return {'status':'OK','data':[]}
        return get,calls

    def test_pagination_failure_preserves_first_page_and_marks_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            transport,calls=self.transport('error'); journal=Journal(directory)
            result=ExtendedReader(transport=transport).sync(journal,start_ms=0)
            self.assertFalse(result[0]['complete'])
            self.assertEqual(result[0]['reason'],'http_429')
            self.assertEqual(len([r for r in journal.records() if r['kind']=='fill']),1)

    def test_cursor_loop_and_page_budget_are_not_complete(self):
        for mode,pages in (('loop',3),('done',1)):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                transport,calls=self.transport(mode)
                result=ExtendedReader(transport=transport).sync(Journal(directory),start_ms=0,max_pages=pages)
                self.assertFalse(result[0]['complete'])
                self.assertLessEqual(len([c for c in calls if c[0]=='trades']),pages)

    def test_end_of_history_and_missing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            transport,_=self.transport('done')
            self.assertTrue(ExtendedReader(transport=transport).sync(Journal(directory),start_ms=0)[0]['complete'])
        with self.assertRaisesRegex(ReadError,'api_key_missing'):
            ExtendedReader(api_key='').get('trades')
        with self.assertRaisesRegex(ReadError,'endpoint_not_allowed'):
            ExtendedReader(api_key='unused').get('create_order')

    def test_network_request_is_get_to_official_host_without_key_in_url(self):
        response=types.SimpleNamespace(read=lambda limit:b'{"status":"OK","data":[]}')
        from contextlib import nullcontext
        captured=[]
        opener=types.SimpleNamespace(open=lambda request,**kw: captured.append(request) or nullcontext(response))
        with patch('bot_core.exchange_history.build_opener',return_value=opener):
            ExtendedReader(api_key='secret').get('trades',{'limit':10})
        self.assertEqual(captured[0].method,'GET')
        self.assertTrue(captured[0].full_url.startswith('https://api.starknet.extended.exchange/api/v1/user/trades?'))
        self.assertNotIn('secret',captured[0].full_url)
        with self.assertRaisesRegex(ReadError,'redirect_blocked'):
            _NoRedirect().redirect_request(None,None,302,'',{},'https://unrelated.invalid')


class ReconciliationTests(unittest.TestCase):
    def test_deposit_is_not_profit(self):
        result=reconciliation('115.71','160.60',external_cashflow='44.89',realized_pnl=0,
            fees=0,funding=0,other_adjustments=0,unrealized_change=0,coverage_complete=True)
        self.assertEqual(Decimal(result['cashflow_adjusted_equity_change']),0)
        self.assertEqual(result['state'],'matched')

    def test_missing_fees_or_partial_history_does_not_reconcile(self):
        result=reconciliation(200,100,external_cashflow=0,realized_pnl=-100,
            funding=0,other_adjustments=0,unrealized_change=0,coverage_complete=True)
        self.assertEqual(result['state'],'incomplete')
        self.assertIn('fees',result['missing'])
        self.assertIsNone(result['residual'])

    def test_known_components_expose_unexplained_difference(self):
        result=reconciliation(200,100,external_cashflow=0,realized_pnl=-90,
            fees=-1,funding=-2,other_adjustments=0,unrealized_change=-5,coverage_complete=True)
        self.assertEqual(Decimal(result['residual']),-2)
        self.assertEqual(result['state'],'unexplained_difference')


class BotHookTests(unittest.TestCase):
    def test_journal_failure_cannot_change_existing_logger_result(self):
        for succeeds in (False,True):
            env=load_definitions({'write_jsonl'},{'__file__':str(ROOT/'BOT5-2-414 .py'),
                '_audit_observe_bot_event':lambda *a:(_ for _ in ()).throw(OSError('disk full')),
                '_ORIG_write_jsonl':lambda *a,**k:succeeds, '_route_log_path':lambda p:p,
                '_cleanup_retention_target':lambda *a:None,
                '_ULOG':types.SimpleNamespace(mirror_json_event=lambda *a,**k:None)})
            self.assertIs(env['write_jsonl']('executor_results.jsonl',{'ok':False}),succeeds)

    def test_stale_scan_is_not_recorded_as_accepted_observation(self):
        observed=[]
        env=load_definitions({'_m2_accept_snapshot'},{'__file__':str(ROOT/'BOT5-2-414 .py'),
            '_SNAPSHOT_COMMIT_LOCK':threading.RLock(),'_pw_runtime_gen_value':lambda:2,
            '_m2_make_fail_snapshot':lambda *a,**k:{'data_quality':'fail'},
            '_snapshot_positions_verified':lambda s:True,'copy':copy,'time':time,
            '_audit_observe_snapshot':lambda *a:observed.append(a),
            'LAST_GOOD_SNAPSHOT':{},'LAST_GOOD_TS':0,'LAST_GOOD_REASON':''})
        env['_m2_accept_snapshot']({'ts':1},1)
        self.assertEqual(observed,[])
        snapshot={'ts':2};self.assertIs(env['_m2_accept_snapshot'](snapshot,2),snapshot)
        self.assertEqual(len(observed),1)


if __name__=='__main__':
    unittest.main()
