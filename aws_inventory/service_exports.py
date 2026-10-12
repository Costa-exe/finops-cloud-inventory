"""Per-service Excel data with readable and plain offline download variants."""
from __future__ import annotations

import base64
from collections import defaultdict
from decimal import Decimal
from io import BytesIO
from math import ceil
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as letter
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.views import Selection

from .workbooks import _text
from .localization import translate_message

MONEY = '"USD "#,##0.00;[Red]("USD "#,##0.00);"USD "0.00'
HEADER = 3


def known_sum(values):
    known = [Decimal(str(v)) for v in values if v is not None]
    return sum(known, Decimal(0)) if known else None


def own(resource, month):
    value = resource['months'][month]
    return value['amount'] if value['currency'] == 'USD' else None


def service_keys(model):
    keys = set(model['context'].get('inventory_services', [])) | {r['service'] for r in model['resources']}
    mapped = {name for key in keys for name in model['billing_services'].get(key, [])}
    return sorted(keys | {'billing:' + b['service'] for b in model['billing'] if b['service'] not in mapped})


def text_cell(cell, value):
    # Force strings, including strings starting with =, to remain literal Excel text.
    cell.value = _text(value)[:32767]
    cell.data_type = 's'


def _service_book(model, service):
    """Every sheet has calendar-month columns, a region field and totals.

    Attached subtotals can overlap. Hidden membership/unique-cost sheets calculate
    the footer from the union of components of the currently visible parents.
    """
    language = model['context'].get('language', 'en')
    tr = lambda en, italian: translate_message(en, italian, language)
    months = [w['month'] for w in model['windows']]
    resources = [r for r in model['resources'] if r['service'] == service]
    index = {r['key']: r for r in model['resources']}
    names = [service[8:]] if service.startswith('billing:') else model['billing_services'].get(service, [])
    bills = [b for b in model['billing'] if b['service'] in names and b['currency'] == 'USD']
    book = Workbook()
    book.remove(book.active)
    book.calculation = CalcProperties(calcId=0, fullCalcOnLoad=True, forceFullCalc=True)
    book.properties.creator = 'AWS Inventory'
    book.properties.title = service.upper() + ' · USD'
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    body_font = Font(name='Calibri', size=11, color='344454')
    heading_font = Font(name='Calibri', size=11, bold=True, color='344454')
    title_font = Font(name='Calibri', size=18, bold=True, color='344454')
    title_fill = PatternFill('solid', fgColor='E5EBF1')
    header_fill = PatternFill('solid', fgColor='E4ECE9')
    stripe_fills = (PatternFill('solid', fgColor='F5F7F9'), PatternFill('solid', fgColor='FFFFFF'))
    edge = Side(style='thin', color='DFE5EB')
    cell_border = Border(left=edge, right=edge, top=edge, bottom=edge)

    def sheet(title, columns, rows):
        ws = book.create_sheet(title)
        money_start = len(columns) + 1
        end_col = money_start + len(months)
        if len(rows) > 1_048_576 - HEADER - 2 or end_col > 16384:
            raise ValueError('Service export exceeds Excel worksheet limits.')
        ws.sheet_view.showGridLines = True
        ws.sheet_view.view = 'normal'
        ws.sheet_view.zoomScale = 90
        # Freeze headers only: wide frozen metadata columns can hide the scrollable area.
        ws.freeze_panes = f'A{HEADER + 1}'
        ws.sheet_view.selection = [Selection(pane='bottomLeft', activeCell='A4', sqref='A4')]
        for row, value in [(1, service.upper() + ' · ' + title), (2, f"{tr('Account', 'Account')} {model['account']}")]:
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=end_col)
            cell = ws.cell(row, 1)
            text_cell(cell, value)
            cell.font = title_font if row == 1 else body_font
            cell.fill = title_fill if row == 1 else stripe_fills[0]
            cell.alignment = center
            ws.row_dimensions[row].height = 36 if row == 1 else 30
        headers = columns + months + [tr('Period total', 'Totale periodo')]
        for col, value in enumerate(headers, 1):
            cell = ws.cell(HEADER, col)
            text_cell(cell, value)
            cell.fill = header_fill
            cell.border = cell_border
            cell.font = heading_font
            cell.alignment = center
            width = 21 if col >= money_start else (32 if col == 1 else 26)
            if col < money_start:
                longest = max((len(str(identity[col - 1])) for identity, _ in rows), default=0)
                width = min(48, max(width, longest + 2))
            ws.column_dimensions[letter(col)].width = width
        ws.row_dimensions[HEADER].height = 32
        for rownum, (identity, amounts) in enumerate(rows, HEADER + 1):
            for col, value in enumerate(identity, 1):
                text_cell(ws.cell(rownum, col), value)
            for col, value in enumerate(amounts, money_start):
                if value is not None:
                    ws.cell(rownum, col, Decimal(str(value)))
            start, end = letter(money_start), letter(end_col - 1)
            ws.cell(rownum, end_col, f'=IF(COUNT({start}{rownum}:{end}{rownum})=0,"",SUM({start}{rownum}:{end}{rownum}))')
            for col in range(1, end_col + 1):
                cell = ws.cell(rownum, col)
                cell.font = body_font
                cell.border = cell_border
                cell.fill = stripe_fills[rownum % 2]
                cell.alignment = center
                if col >= money_start:
                    cell.number_format = MONEY
            lines = max((sum(max(1, ceil(len(line) / max(1, ws.column_dimensions[letter(col)].width - 3)))
                             for line in str(value).split('\n'))
                         for col, value in enumerate(identity, 1)), default=1)
            ws.row_dimensions[rownum].height = min(409, max(36, lines * 16 + 12))
        last = HEADER + len(rows)
        footer = max(HEADER + 2, last + 2)
        text_cell(ws.cell(footer, 1), tr('Total', 'Totale'))
        for col in range(money_start, end_col + 1):
            name = letter(col)
            formula = f'=IF(SUBTOTAL(102,{name}{HEADER + 1}:{name}{last})=0,"",SUBTOTAL(109,{name}{HEADER + 1}:{name}{last}))' if rows else '=""'
            cell = ws.cell(footer, col, formula)
            cell.number_format = MONEY
        for cell in ws[footer][:end_col]:
            cell.fill = title_fill
            cell.border = cell_border
            cell.font = heading_font
            cell.alignment = center
        ws.row_dimensions[footer].height = 36
        ws.print_title_rows = f'1:{HEADER}'
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.paperSize = ws.PAPERSIZE_A3
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        return ws, money_start, footer

    billed = defaultdict(lambda: defaultdict(list))
    for b in bills:
        billed[(b['region'], b['metric'], b['service'])][b['month']].append(b['amount'])
    sheet(tr('Service spending', 'Spesa servizi'), [tr('Region', 'Regione'), tr('Metric', 'Metrica'), tr('Billing service', 'Servizio fatturato')],
          [(list(key), [known_sum(values[m]) for m in months]) for key, values in sorted(billed.items())])
    grouped = defaultdict(list)
    for r in resources:
        grouped[(r['region'], r['instance_type'] or r['storage_type'] or r['type'])].append(r)
    sheet(tr('By type', 'Per tipo'), [tr('Region', 'Regione'), tr('Instance or resource type', 'Tipo istanza o risorsa'), tr('Coverage', 'Copertura')],
          [([region, kind, tr('Known own costs only', 'Solo costi propri noti')], [known_sum(own(r, m) for r in rs) for m in months]) for (region, kind), rs in sorted(grouped.items())])
    def cost_note(r):
        entries = list(r['months'].values())
        estimated = any(v['basis'] in {'estimated', 'mixed'} for v in entries)
        return tr('Includes estimates', 'Include stime') if estimated else tr('Billed / unavailable', 'Fatturato / non disponibile')
    sheet(tr('By resource', 'Per risorsa'), [tr('Name', 'Nome'), tr('Region', 'Regione'), tr('Type', 'Tipo'), tr('Catalog rate', 'Tariffa catalogo'), tr('Basis', 'Fonte')],
          [([r['name'] or r['id'], r['region'], r['instance_type'] or r['storage_type'] or r['type'], r['plan']['rate_label'] if r['plan']['currency'] == 'USD' else '', cost_note(r)], [own(r, m) for m in months]) for r in resources])
    attached_rows = []
    for r in resources:
        children = [index[k] for k in r['included_keys'] if k != r['key']]
        included = ', '.join(sorted({c['service'].upper() + ' / ' + c['type'] for c in children}))
        included = tr('Includes: ', 'Include: ') + included if included else tr('Own resource only', 'Solo risorsa propria')
        if any(not v['complete'] for v in r['totals'].values()):
            included += tr(' · incomplete known subtotal', ' · subtotale noto incompleto')
        attached_rows.append(([r['name'] or r['id'], r['region'], r['instance_type'] or r['type'], included],
                              [r['totals'][m]['amounts'].get('USD') for m in months]))
    attached, start, footer = sheet(tr('Attached costs', 'Costi associati'), [tr('Name', 'Nome'), tr('Region', 'Regione'), tr('Type', 'Tipo'), tr('Included services', 'Servizi inclusi')], attached_rows)
    text_cell(attached.cell(footer, 1), tr('Unique total', 'Totale univoco'))
    # Membership rows refer to visible parent cells; unique component rows only
    # contribute if at least one visible parent includes that component.
    membership = book.create_sheet('_members')
    unique = book.create_sheet('_unique')
    membership.sheet_state = unique.sheet_state = 'hidden'
    included_keys = set()
    quoted_title = "'" + attached.title.replace("'", "''") + "'"
    for rownum, r in enumerate(resources, HEADER + 1):
        for key in r['included_keys']:
            included_keys.add(key)
            membership.append([key, f'=SUBTOTAL(103,{quoted_title}!A{rownum})'])
    last_member = max(1, membership.max_row)
    for key in sorted(included_keys):
        rownum = unique.max_row + 1 if unique.cell(1, 1).value is not None else 1
        text_cell(unique.cell(rownum, 1), key)
        for col, m in enumerate(months, 2):
            amount = own(index[key], m)
            if amount is not None:
                unique.cell(rownum, col, f'=IF(SUMIF(_members!$A$1:$A${last_member},A{rownum},_members!$B$1:$B${last_member})>0,{Decimal(str(amount)):f},"")')
    for offset in range(len(months)):
        col = letter(offset + 2)
        rng = f'_unique!{col}1:{col}{max(1, unique.max_row)}'
        attached.cell(footer, start + offset, f'=IF(COUNT({rng})=0,"",SUM({rng}))')
    rng = f'{letter(start)}{footer}:{letter(start + len(months) - 1)}{footer}'
    attached.cell(footer, start + len(months), f'=IF(COUNT({rng})=0,"",SUM({rng}))')
    return book


def _plain(book):
    """Remove presentation only; keep coordinates, values, types and formulas."""
    for sheet in book:
        for merged in list(sheet.merged_cells.ranges):
            sheet.unmerge_cells(str(merged))
        for row in sheet:
            for cell in row:
                cell.style = 'Normal'
        for row in sheet.row_dimensions.values():
            row.height = None
        sheet.freeze_panes = None
        sheet.sheet_view.selection = [Selection(activeCell='A1', sqref='A1')]
        sheet.sheet_view.showGridLines = True
        sheet.sheet_view.zoomScale = 100


def _serialize(book):
    with BytesIO() as output:
        book.save(output)
        return output.getvalue()


def build_service_workbook(model, service, *, style='readable'):
    if style not in {'readable', 'basic'}:
        raise ValueError('Excel style must be readable or basic.')
    book = _service_book(model, service)
    try:
        if style == 'basic':
            _plain(book)
        return _serialize(book)
    finally:
        book.close()


def embedded_workbooks(model):
    result = {}
    for service in service_keys(model):
        slug = re.sub(r'[^a-zA-Z0-9_-]', '_', service)[:100]
        prefix = f'aws-{"simulation-" if model["simulation_only"] else ""}{slug}'
        book = _service_book(model, service)
        try:
            variants = {}
            # Calculate cells and formulas once, then serialize the same data twice.
            for style in ('readable', 'basic'):
                if style == 'basic':
                    _plain(book)
                variants[style] = {'filename': f'{prefix}-{style}.xlsx',
                                   'base64': base64.b64encode(_serialize(book)).decode('ascii')}
            result[service] = variants
        finally:
            book.close()
    return result
