"""Small read-only browser probes. Importing this module does no browser work."""


def pagination_detected(candidates):
    """Site navigation's aria-current=page is not a table page selector."""
    if not isinstance(candidates, list):
        return True
    for item in candidates:
        if not isinstance(item, dict) or 'visible' not in item:
            return True
        if item['visible'] is False:
            continue
        label = str(item.get('label') or '').casefold()
        marker = ' '.join(str(item.get(k) or '') for k in ('label', 'testid', 'class_name')).casefold()
        if 'pagination' in marker or item.get('in_pagination_container') is True:
            return True
        if str(item.get('tag') or '').upper() == 'BUTTON' and ('page' in label or label in ('next', 'previous')):
            return True
        if item.get('aria_current') == 'page' and str(item.get('text') or '').strip().isdigit():
            return True
    return False


def coverage_details_verified(details, parsed_count):
    return bool(parsed_count > 0 and isinstance(details, dict)
                and details.get('table_ready') is True
                and details.get('virtualized') is False
                and details.get('pagination_present') is False
                and details.get('all_rows_visible') is True
                and details.get('scope_all') is True
                and details.get('declared_count_matches') is True
                and details.get('row_count') == parsed_count)


def table_coverage_verified(page, cfg, parsed_count):
    """Require a loaded, nonvirtualized, unpaginated table with every row parsed.

    Partial/virtualized observations can prove presence, never absence. A future
    exchange-specific paginated adapter can provide its own stronger evidence.
    """
    if parsed_count <= 0:
        return False
    try:
        details = page.locator(cfg['rows']).evaluate_all(r'''(rows, platform) => {
            if (!rows.length || !document.body) return {complete:false};
            const table = rows[0].closest('table');
            if (!table || table.getAttribute('aria-busy') === 'true') return {complete:false};
            // An unrelated virtualized market picker is not the positions table.
            const virtualSelector = 'svelte-virtual-list-viewport, [data-virtualizer], [data-virtualized="true"]';
            const virtual = table.closest(virtualSelector) || table.querySelector(virtualSelector);
            const declared = Number(table.getAttribute('aria-rowcount') || 0);
            // Last-page Next=disabled is not evidence of a single-page table.
            // Pagination may live next to the table, not inside its parent.
            const paginationContainer = '[aria-label*="pagination" i], [data-testid*="pagination" i], [class*="pagination" i]';
            const pagination = Array.from(document.querySelectorAll(paginationContainer + ', [aria-current="page"], button[aria-label*="page" i], button[aria-label="Next" i], button[aria-label="Previous" i]'));
            const visibleRows = rows.filter(r => r.getClientRects().length > 0);
            const scopeAll = platform === 'VAR' || (platform === 'EXT' &&
                Array.from(document.querySelectorAll('input[type="radio"]')).some(e =>
                    e.checked && (e.value === 'ALL' || e.id.endsWith(':ALL'))));
            return {table_ready: true, virtualized: !!virtual,
                scope_all: scopeAll,
                pagination_candidates: pagination.map(e => ({tag:e.tagName, label:e.getAttribute('aria-label'),
                    testid:e.getAttribute('data-testid'), class_name:e.getAttribute('class'),
                    aria_current:e.getAttribute('aria-current'), text:e.innerText,
                    visible:e.getClientRects().length > 0, in_pagination_container:!!e.closest(paginationContainer)})),
                declared_count_matches: Number.isFinite(declared) && (!declared || declared === rows.length || declared === rows.length + 1),
                all_rows_visible: visibleRows.length === rows.length, row_count: rows.length};
        }''', str(cfg.get('name') or '').upper())
        if isinstance(details, dict):
            details['pagination_present'] = pagination_detected(details.get('pagination_candidates'))
        return coverage_details_verified(details, parsed_count)
    except Exception:
        return False
