"""Monthly Excel downloads, shared costs and descriptive resource links."""
from decimal import Decimal
from io import BytesIO
import unittest

from openpyxl import load_workbook

from aws_inventory.service_exports import build_service_workbook, embedded_workbooks
from tests.test_html_report import build, resource, quote


class ServiceExportTests(unittest.TestCase):
    def model(self):
        return build([resource('i-a', resource_name='API', instance_type='t3.large'),
                      resource('i-b', resource_name='Worker', instance_type='t3.large'),
                      resource('vol-a', 'ebs', 'volume', storage_type='gp2', storage_gib=100,
                               attached_to=['i-a', 'i-b'], resource_name='Shared data'), quote('vol-a')],
                     language='it', period_start='2026-08-01')

    def test_four_styled_sheets_months_without_dropdowns_and_separate_own_costs(self):
        workbook = load_workbook(BytesIO(build_service_workbook(self.model(), 'ec2')))
        visible = [ws for ws in workbook if ws.sheet_state == 'visible']
        self.assertEqual([ws.title for ws in visible], ['Spesa servizi', 'Per tipo', 'Per risorsa', 'Costi associati'])
        for ws in visible:
            headings = [c.value for c in ws[3]]
            self.assertIn('Regione', headings)
            self.assertIn('2026-08', headings)
            self.assertIn('2026-09', headings)
            self.assertIn('Totale periodo', headings)
            self.assertIsNone(ws.auto_filter.ref)
            self.assertEqual(len(ws.data_validations.dataValidation), 0)
            self.assertTrue(ws.freeze_panes.endswith('4'))
            self.assertEqual(ws['A2'].value, 'Account 123456789012')
            self.assertEqual(ws.print_title_rows, '$1:$3')
        own, attached = workbook['Per risorsa'], workbook['Costi associati']
        self.assertEqual(own['F4'].value, 89.28)
        self.assertEqual(attached['E4'].value, 97.28)
        self.assertIn('EBS', attached['D4'].value)
        self.assertEqual(own['F7'].value, '=IF(SUBTOTAL(102,F4:F5)=0,"",SUBTOTAL(109,F4:F5))')
        self.assertEqual(len([row for row in workbook['_unique'] if row[0].value]), 3)
        self.assertIn('_unique', attached['E7'].value)
        self.assertIn('SUBTOTAL(103', workbook['_members']['B1'].value)
        self.assertIn('SUMIF', workbook['_unique']['B1'].value)

    def test_unknown_costs_are_blank_and_names_are_literal_text(self):
        model = build([resource('fn', 'lambda', 'function', resource_name='=HYPERLINK("evil")',
                                list_price_per_hour=None)], language='it')
        workbook = load_workbook(BytesIO(build_service_workbook(model, 'lambda')))
        self.assertEqual(workbook['Per risorsa']['A4'].data_type, 's')
        self.assertEqual(workbook['Per risorsa']['A4'].value, '=HYPERLINK("evil")')
        self.assertIsNone(workbook['Per risorsa']['F4'].value)
        self.assertIsNone(workbook['Costi associati']['E4'].value)

    def test_billing_only_service_and_native_currency_are_preserved(self):
        bill = {'row_type': 'cost_summary', 'service': 'Special AWS service', 'region': 'us-east-1',
                'period_start': '2026-09-01', 'period_end': '2026-10-01', 'amount': '-5.125', 'unit': 'USD', 'metric': 'AmortizedCost'}
        model = build([bill, {**bill, 'unit': 'EUR', 'amount': '100'}])
        exports = embedded_workbooks(model)
        self.assertIn('billing:Special AWS service', exports)
        workbook = load_workbook(BytesIO(build_service_workbook(model, 'billing:Special AWS service')))
        self.assertEqual(workbook['Service spending']['D4'].value, -5.125)
        self.assertEqual(workbook['Service spending']['A4'].value, 'us-east-1')

    def test_named_typed_links_keep_reference_and_dependency_distinct(self):
        model = build([resource('i-a', resource_name='API', security_group_ids=['sg-a'], related_resource_ids=['vol-a']),
                       resource('vol-a', 'ebs', 'volume', attached_to=['i-a'], storage_gib=100), quote('vol-a'),
                       resource('sg-a', 'vpc', 'security_group', resource_name='Web security', list_price_per_hour=None)])
        volume = next(r for r in model['resources'] if r['id'] == 'vol-a')
        machine = next(r for r in model['resources'] if r['id'] == 'i-a')
        ref = next(r for r in volume['relations'] if r['id'] == 'i-a')
        self.assertEqual((ref['name'], ref['type']), ('API', 'instance'))
        sg = next(r for r in machine['relations'] if r['id'] == 'sg-a')
        self.assertEqual((sg['name'], sg['type']), ('Web security', 'security_group'))
        self.assertNotIn(sg['key'], machine['included_keys'])
        self.assertIn(volume['key'], machine['included_keys'])
        self.assertEqual(machine['totals']['2026-09']['amounts']['USD'], Decimal('94.4'))
