"""Deployment quantities and catalog ambiguity must not fabricate cheap estimates."""
from copy import deepcopy
from decimal import Decimal
from unittest import TestCase

from aws_inventory.estimates import full_rate
from aws_inventory.pricing import catalog_query
from tests.test_html_report import build, quote, resource


class EstimateEdgeCasesTests(TestCase):
    def test_eks_auto_mode_marks_unpriced_surcharge_as_incomplete(self):
        rate = quote('catalog', '0.10', 'Hrs', 'eks', resource_type='eks_regional_catalog',
                     price_component='EKS standard support')
        for enabled in (False, True):
            r = build([resource('cluster', 'eks', 'cluster',
                                details={'compute_config': {'enabled': enabled}}), rate])['resources'][0]
            self.assertEqual(r['months']['2026-09']['amount'], 72)
            self.assertEqual(r['months']['2026-09']['complete'], not enabled)
            self.assertEqual(r['totals']['2026-09']['complete'], not enabled)

    def test_redshift_multi_az_counts_both_zones(self):
        for deployment, expected in [('Disabled', '172.8'), ('Enabled', '345.6')]:
            with self.subTest(deployment=deployment):
                r = build([resource('warehouse', 'redshift', 'cluster',
                                   details={'number_of_nodes': 2, 'multi_az': deployment})])['resources'][0]
                self.assertEqual(r['months']['2026-09']['amount'], Decimal(expected))
                self.assertFalse(r['months']['2026-09']['complete'])  # Storage and usage excluded.

    def test_redshift_creation_and_runtime_apply_after_node_quantity(self):
        r = build([resource('warehouse', 'redshift', 'cluster', created_at='2026-09-16',
                            details={'number_of_nodes': 2, 'multi_az': 'Enabled'})],
                  estimate_hours_per_day='8')['resources'][0]
        self.assertEqual(r['months']['2026-09']['amount'], Decimal('57.6'))

    def test_unknown_redshift_deployment_is_not_a_single_zone_estimate(self):
        for deployment in (None, 'Modifying', True):
            with self.subTest(deployment=deployment):
                r = build([resource('warehouse', 'redshift', 'cluster',
                                   details={'number_of_nodes': 2, 'multi_az': deployment})])['resources'][0]
                self.assertIsNone(r['months']['2026-09']['amount'])
                self.assertFalse(r['months']['2026-09']['complete'])

    def test_rds_requires_known_deployment_without_guessing_single_az(self):
        for deployment in (None, 'false', 0):
            row = resource('db', 'rds', 'db_instance', instance_type='db.m5.large',
                           engine='postgres', multi_az=deployment)
            self.assertIsNone(catalog_query(row))
            self.assertEqual(row['price_status'], 'missing_database_deployment')
        for deployment, expected in [(False, 'Single-AZ'), (True, 'Multi-AZ')]:
            row = resource('db', 'rds', 'db_instance', instance_type='db.m5.large',
                           engine='postgres', multi_az=deployment)
            self.assertEqual(catalog_query(row)[1]['deploymentOption'], expected)

    def test_aurora_cluster_az_layout_is_not_a_multi_az_instance_rate(self):
        for deployment in (None, False, True):
            row = resource('db', 'rds', 'db_instance', instance_type='db.r6g.large',
                           engine='aurora-postgresql', multi_az=deployment, cluster_name='cluster')
            self.assertEqual(catalog_query(row)[1]['deploymentOption'], 'Single-AZ')

    def test_distinct_equal_price_dimensions_are_not_collapsed(self):
        first = quote('lb', '0.025', 'Hrs', 'elb', details={'rate_code': 'first'})
        self.assertIsNotNone(full_rate([first, deepcopy(first)]))
        for change in ({'details': {'rate_code': 'second'}},
                       {'price_description': 'Different billed component'},
                       {'price_effective_date': '2025-01-01T00:00:00Z'},
                       {'price_unit': 'hours'}):
            with self.subTest(change=change):
                second = {**first, **change}
                self.assertIsNone(full_rate([first, second]))
                r = build([resource('lb', 'elb', 'load_balancer'), first, second])['resources'][0]
                self.assertIsNone(r['months']['2026-09']['amount'])

    def test_ambiguous_or_tiered_quotes_cannot_fall_back_to_a_cached_scalar(self):
        first = quote('i-one', '0.10', 'Hrs', 'ec2')
        cases = [[first, {**first, 'price_sku': 'alternative', 'price_per_unit': '0.20'}],
                 [{**first, 'price_end_range': '100'}]]
        for rates in cases:
            with self.subTest(rates=rates):
                r = build([resource(), *rates])['resources'][0]
                self.assertIsNone(r['months']['2026-09']['amount'])
        self.assertEqual(build([resource()])['resources'][0]['months']['2026-09']['amount'], Decimal('86.4'))

    def test_fargate_spot_is_explicitly_an_on_demand_comparison(self):
        row = resource('task', 'ecs', 'task', vcpu=1, memory_mib=2048,
                       details={'launch_type': 'FARGATE', 'capacity_provider': 'FARGATE_SPOT',
                                'operating_system_family': 'LINUX', 'cpu_architecture': 'X86_64'})
        rates = [quote('task', rate, 'Hrs', 'ecs', price_sku=component,
                       price_description='Fargate ' + component)
                 for component, rate in [('vcpu', '0.04'), ('gb-hours', '0.004')]]
        r = build([row, *rates])['resources'][0]
        self.assertEqual(r['months']['2026-09']['amount'], Decimal('34.56'))
        self.assertIn('Spot resource: estimate uses On-Demand comparison, not the Spot rate.', r['plan']['notes'])
