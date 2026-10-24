"""Documented account-history readers. Extended uses GET only; VAR uses CSV.

No wallet/private key, browser session, order API, or trading imports are used.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .account_journal import event, number, timestamp

EXT_DOCUMENTATION = 'https://api.docs.extended.exchange/'
VAR_DOCUMENTATION = 'https://docs.variational.io/technical-documentation/trade-and-transfer-history'
EXT_BASE = 'https://api.starknet.extended.exchange/api/v1'
EXT_PATHS = {'account': '/user/account/info', 'balance': '/user/balance',
             'trades': '/user/trades', 'funding': '/user/funding/history',
             'transfers': '/user/assetOperations', 'positions': '/user/positions/history',
             'orders': '/user/orders/history', 'assets': '/info/assets'}


def _identifier(value):
    return str(value).strip() if value is not None and not isinstance(value, bool) else ''


def _positive(value):
    result = number(value)
    return result if result is not None and Decimal(result) > 0 else None


def _cash_for_transfer(kind, amount, *, signed=False):
    value = number(amount)
    if value is None:
        return None
    amount = Decimal(value)
    if amount == 0:
        return '0'
    if signed:
        return str(amount)
    if kind == 'deposit' and amount > 0:
        return str(amount)
    if kind == 'withdrawal':
        return str(-abs(amount))
    return None  # Funding/PnL sign conventions must be verified, never guessed.


def normalize_ext(channel, row, account, *, asset_names=None):
    if not isinstance(row, dict):
        raise ValueError('exchange row must be an object')
    if row.get('accountId') is not None and _identifier(row['accountId']) != str(account):
        raise ValueError('account mismatch')
    source_id = _identifier(row.get('id'))
    if not source_id:
        raise ValueError('exchange record ID missing')
    common = {'order_id': _identifier(row.get('orderId')) or None,
              'client_order_id': _identifier(row.get('externalId') or row.get('externalOrderId')) or None,
              'market': row.get('market'), 'side': row.get('side'), 'currency': 'USDC'}
    records = []
    def add(kind, data, when, suffix=''):
        records.append(event('EXT', account, kind, source_id + suffix, source='extended_api',
            authority='exchange_record', occurred_at=timestamp(when), data={**common, **data}, raw=row))
    if channel == 'trades':
        qty = _positive(row.get('filledQty', row.get('qty')))
        price = _positive(row.get('averagePrice', row.get('price')))
        valid = qty is not None and price is not None and timestamp(row.get('createdTime')) is not None
        add('fill', {'status': 'confirmed' if valid else 'unverified', 'quantity': qty,
            'price': price, 'notional': number(row.get('value')), 'fee_reported': number(row.get('fee')),
            'trade_type': row.get('tradeType'), 'is_taker': row.get('isTaker'),
            'liquidation': row.get('tradeType') in ('LIQUIDATION', 'DELEVERAGE'),
            'missing_fields': [] if valid else ['fill_quantity_price_or_time']}, row.get('createdTime'))
        fee = number(row.get('fee'))
        add('fee', {'status': 'confirmed' if valid else 'unverified', 'trade_id': source_id,
            'amount_reported': fee, 'cash_delta': str(-Decimal(fee)) if fee is not None else None,
            'sign_basis': 'documented_paid_fee', 'missing_fields': ['fee'] if fee is None else []}, row.get('createdTime'), ':fee')
    elif channel == 'funding':
        value = number(row.get('fundingFee'))
        add('funding', {'status': 'confirmed' if value is not None and timestamp(row.get('paidTime')) else 'unverified', 'amount_reported': value,
            'cash_delta': '0' if value is not None and Decimal(value) == 0 else None,
            'funding_rate': number(row.get('fundingRate')), 'position_id': _identifier(row.get('positionId')),
            'sign_basis': 'unknown_API_documentation_only_says_payment_size'}, row.get('paidTime'))
    elif channel == 'transfers':
        kind = {'DEPOSIT': 'deposit', 'WITHDRAWAL': 'withdrawal', 'TRANSFER': 'transfer'}.get(row.get('type'), 'adjustment')
        asset = row.get('asset')
        currency = (asset_names or {}).get(str(asset))
        if currency is None and isinstance(asset, str) and asset.isalpha():
            currency = asset.upper()
        amount = number(row.get('amount'))
        state = {'COMPLETED': 'confirmed', 'REJECTED': 'failed'}.get(row.get('status'), 'pending')
        if state == 'confirmed' and (amount is None or timestamp(row.get('time')) is None):
            state = 'unverified'
        add(kind, {'status': state, 'currency': currency, 'asset_id': asset,
            'amount_reported': amount, 'cash_delta': _cash_for_transfer(kind, amount) if currency else None,
            'transfer_fee_reported': number(row.get('fee')), 'transfer_fee_inclusion': 'not_verified',
            'transaction_hash': row.get('transactionHash'), 'chain': row.get('chain'),
            'counterparty_account_id': _identifier(row.get('counterpartyAccountId')) or None}, row.get('time'))
    elif channel == 'positions':
        add('position_summary', {'status': 'reference_only', 'position_id': source_id,
            'realized_pnl_reported': number(row.get('realisedPnl')), 'size': number(row.get('size')),
            'open_price': number(row.get('openPrice')), 'exit_price': number(row.get('exitPrice')),
            'closed_at': timestamp(row.get('closedTime')), 'exit_type': row.get('exitType'),
            'cash_delta': None, 'note': 'Cumulative position summary; not added to per-event cash flows.'},
            row.get('closedTime') or row.get('createdTime'))
    elif channel == 'orders':
        add('order', {'status': str(row.get('status') or 'UNKNOWN').lower(),
            'order_id': source_id, 'client_order_id': row.get('externalId'),
            'requested_qty': number(row.get('qty')), 'filled_qty_reported': number(row.get('filledQty')),
            'reason': row.get('statusReason'), 'note': 'Order status is not a separate fill.'},
            row.get('updatedTime') or row.get('createdTime'))
    else:
        raise ValueError('unsupported Extended history channel')
    return records


def normalize_var(row, account, *, channel, naive_timezone=None, quantity_convention='unknown'):
    source_id = _identifier(row.get('id'))
    if not source_id:
        raise ValueError('CSV record ID missing')
    stamp = timestamp(row.get('created_at'), naive_timezone=naive_timezone)
    common = {'market': row.get('underlying'), 'instrument_type': row.get('instrument_type'),
              'status': str(row.get('status') or 'unknown').lower(), 'order_id': None,
              'time_verified': stamp is not None}
    if channel == 'trades':
        quantity, price = _positive(row.get('qty')), _positive(row.get('price'))
        if common['status'] == 'confirmed' and (stamp is None or quantity is None or price is None):
            common['status'] = 'unverified'
        kind = 'fill'
        data = {**common, 'quantity': quantity, 'price': price,
                'side': str(row.get('side') or '').upper(), 'trade_type': row.get('trade_type'),
                'liquidation': row.get('trade_type') == 'liquidation',
                'liquidation_trigger_price': number(row.get('liquidation_trigger_price')),
                'fee_reported': None, 'fee_source': 'separate_transfers_export'}
    elif channel == 'transfers':
        if quantity_convention not in ('signed', 'unknown'):
            raise ValueError('unknown quantity convention')
        kind = str(row.get('transfer_type') or 'unknown').lower()
        if kind not in ('deposit', 'withdrawal', 'realized_pnl', 'funding', 'fee'):
            kind = 'adjustment'
        data = {**common, 'currency': row.get('asset'), 'amount_reported': number(row.get('qty')),
                'cash_delta': _cash_for_transfer(kind, row.get('qty'), signed=quantity_convention == 'signed'),
                'sign_basis': 'user_verified_signed_qty' if quantity_convention == 'signed' else 'type_or_unknown',
                'funding_rate': number(row.get('funding_rate')), 'fee_type': row.get('fee_type')}
        if stamp is None or data['amount_reported'] is None:
            data['status'] = 'unverified'
    else:
        raise ValueError('unsupported VAR CSV channel')
    return event('VAR', account, kind, source_id, source='variational_csv', authority='exchange_record',
                 occurred_at=stamp, data=data, raw=row)


def import_var_csv(journal, path, *, account, channel, naive_timezone=None, quantity_convention='unknown'):
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    count = added = rejected = 0
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        required = {'id', 'created_at', 'status', 'price', 'qty', 'underlying'} if channel == 'trades' else {'id', 'created_at', 'status', 'qty', 'asset', 'transfer_type'}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError('CSV columns do not match the documented ' + channel + ' export')
        for index, row in enumerate(reader, 2):
            count += 1
            try:
                record = normalize_var(row, account, channel=channel, naive_timezone=naive_timezone,
                                       quantity_convention=quantity_convention)
                added += int(journal.append(record))
            except (ValueError, TypeError):
                rejected += 1
                journal.append(event('VAR', account, 'parse_error', digest + ':' + str(index),
                    source='variational_csv', authority='collection_status', occurred_at=None,
                    data={'row': index, 'channel': channel, 'error': 'record_validation_failed'}, raw=row))
    coverage = {'platform': 'VAR', 'account': account, 'channel': channel,
                'file': path.name, 'sha256': digest, 'rows': count, 'added': added, 'rejected': rejected,
                'complete': False, 'scope': 'exported_rows_only', 'pending_items_excluded': True,
                'row_limit_hit': count >= 10000, 'history_limit_days': 365,
                'quantity_convention': quantity_convention, 'source_documentation': VAR_DOCUMENTATION}
    journal.append(event('VAR', account, 'coverage', 'csv:' + channel + ':' + digest,
        source='variational_csv', authority='collection_status', occurred_at=timestamp(time.time()), data=coverage))
    return coverage


class ReadError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReadError('redirect_blocked')


class ExtendedReader:
    def __init__(self, api_key=None, *, key_env='BOT_AUDIT_EXT_API_KEY', transport=None):
        self._api_key = api_key if api_key is not None else os.environ.get(key_env)
        self._transport = transport

    def get(self, channel, params=None):
        if channel not in EXT_PATHS:
            raise ReadError('endpoint_not_allowed')
        if self._transport:
            payload = self._transport(channel, params or {})
        else:
            if not self._api_key:
                raise ReadError('api_key_missing')
            url = EXT_BASE + EXT_PATHS[channel]
            if params:
                url += '?' + urlencode(params)
            request = Request(url, method='GET', headers={'X-Api-Key': self._api_key,
                'User-Agent': 'bot-account-audit/1.0', 'Accept': 'application/json'})
            try:
                with build_opener(_NoRedirect).open(request, timeout=12) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ReadError('response_too_large')
                payload = json.loads(raw)
            except HTTPError as exc:
                raise ReadError('http_' + str(exc.code)) from None
            except (URLError, TimeoutError, OSError, ValueError):
                raise ReadError('network_or_json_error') from None
        if not isinstance(payload, dict) or payload.get('status') != 'OK' or 'data' not in payload:
            raise ReadError('invalid_api_response')
        return payload

    def sync(self, journal, *, start_ms, max_pages=25):
        if max_pages <= 0 or start_ms < 0:
            raise ValueError('invalid history bounds')
        info = self.get('account')['data']
        account = _identifier(info.get('accountId')) if isinstance(info, dict) else ''
        if not account:
            raise ReadError('account_id_missing')
        asset_names = {}
        try:
            for asset in self.get('assets')['data']:
                if isinstance(asset, dict) and asset.get('name'):
                    asset_names[str(asset.get('id'))] = str(asset['name'])
        except (ReadError, TypeError):
            pass  # Unresolved asset IDs remain unknown; they are not assumed USD.
        reports = []
        for channel in ('trades', 'funding', 'transfers', 'orders', 'positions'):
            page_size = 50 if channel == 'transfers' else 1000
            params = {'limit': page_size}
            if channel == 'funding':
                params['startTime'] = int(start_ms)
            seen = set()
            rows = added = rejected = 0
            complete, reason = False, 'page_budget_exhausted'
            pages = 0
            try:
                for pages in range(1, max_pages + 1):
                    payload = self.get(channel, params)
                    data = payload['data']
                    if not isinstance(data, list):
                        raise ReadError('history_not_a_list')
                    for row in data:
                        rows += 1
                        try:
                            for record in normalize_ext(channel, row, account, asset_names=asset_names):
                                added += int(journal.append(record))
                        except (ValueError, TypeError):
                            rejected += 1
                            key = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                            journal.append(event('EXT', account, 'parse_error', channel + ':' + key,
                                source='extended_api', authority='collection_status', occurred_at=None,
                                data={'channel': channel, 'error': 'record_validation_failed'}, raw=row))
                    cursor = (payload.get('pagination') or {}).get('cursor')
                    if not data:
                        complete, reason = True, 'end_of_history'
                        break
                    if cursor is None:
                        complete, reason = len(data) < page_size, 'no_next_cursor'
                        break
                    marker = str(cursor)
                    if marker in seen:
                        reason = 'cursor_loop'
                        break
                    seen.add(marker)
                    params['cursor'] = cursor
            except ReadError as exc:
                reason = str(exc)  # ReadError contains only our fixed diagnostic codes.
            coverage = {'platform': 'EXT', 'account': account, 'channel': channel, 'rows': rows,
                'added': added, 'rejected': rejected, 'pages': pages,
                'complete': complete and rejected == 0, 'reason': reason,
                'start_ms': start_ms if channel == 'funding' else None,
                'scope': 'API_retained_history', 'source_documentation': EXT_DOCUMENTATION}
            journal.append(event('EXT', account, 'coverage', channel, source='extended_api',
                authority='collection_status', occurred_at=timestamp(time.time()), data=coverage))
            reports.append(coverage)
        try:
            balance = self.get('balance')['data']
            if not isinstance(balance, dict):
                raise ReadError('balance_not_an_object')
            observed = timestamp(time.time())
            journal.append(event('EXT', account, 'equity', observed, source='extended_api',
                authority='exchange_record', occurred_at=timestamp(balance.get('updatedTime')) or observed,
                data={'equity': number(balance.get('equity')), 'balance': number(balance.get('balance')),
                    'unrealized_pnl': number(balance.get('unrealisedPnl')),
                    'available_to_trade': number(balance.get('availableForTrade')),
                    'currency': 'USDC', 'status': 'observation'}, raw=balance))
        except ReadError as exc:
            reports.append({'platform': 'EXT', 'account': account, 'channel': 'balance',
                            'complete': False, 'reason': str(exc)})
        return reports
