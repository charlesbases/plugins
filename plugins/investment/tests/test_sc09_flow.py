"""四个获准的独立producer基础校准；不构造Seal、Host或完整selector。"""

import copy
import os
from pathlib import Path
import unittest
from fractions import Fraction
from types import SimpleNamespace

from investment.contracts import artifact, known, na
from investment.research.contracts import guard_namespace
from investment.chains.recommendation.production_finance import build_estimates, resolve_lots
from investment.chains.recommendation.production_inputs import present
from investment.chains.recommendation.production_publication import (
    build_funding_conditions, intersect_bounds, raw_time_evidence)


class ProducerFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = guard_namespace(Path(os.environ['SC09_FOUNDATION_ROOT']), 'test')
        path = root / 'runs' / 'sc09' / '20261021' / 'foundation' / 'contract.txt'
        source = ('Synthetic fixture for independent producer arithmetic and source-time projection. '
                  'Subscription actual payment includes external fee; net=payment/(1+rate); '
                  'rate=0.01, reference NAV=1.2 dated 2026-10-19; money precision 2, '
                  'share precision 2, final rounding half_up. Confirmed buy lot 1000 shares, '
                  'original first-step sell 200 shares. Expected settlement source date '
                  '2026-10-21, timezone Asia/Shanghai. Declared current plan 100 CNY; '
                  'future contribution 200 CNY dated 2026-10-22 remains conditional.').encode('utf-8')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)
        cls.source_ref = artifact(source, 'investment.research_text',
                                  'investment-run://sc09/20261021/foundation/contract.txt')
        cls.source_ref['media_type'] = 'text/plain'

    def test_subscription_payment_estimates(self):
        # 模拟合同明确统一half_up、金额/份额精度2；独立预期来自1000/(1+.01)。
        citation = {'raw_ref': self.source_ref, 'locator': '/',
                    'quote': 'net=payment/(1+rate)', 'claim': 'Synthetic external subscription fee formula.'}
        terms = [{'term': name, 'value': present('2'), 'text_value': na('numeric_term'), 'citations': [citation]}
                 for name in ('money_precision', 'share_precision')]
        terms.append({'term': 'rounding_rule', 'value': na('text_term'),
                      'text_value': present('half_up'), 'citations': [citation]})
        tier = {'tier_id': 'subscription-tier', 'charge': 'subscription',
                'calculation': 'external_subscription_ratio', 'amount_lower': '0',
                'amount_upper': na('unbounded'), 'rate': present('0.01'), 'currency': present('CNY'),
                'citations': [citation]}
        contract = {'terms': terms, 'fees': [tier]}
        grid = {'money_decimals': present(2), 'share_decimals': present(2), 'rounding_rule': present('half_up')}
        action = {'side': 'buy', 'amount': present('1000.00'), 'fee_tier_ids': ['subscription-tier']}
        observation = {'reference_nav': present('1.2'), 'reference_nav_date': present('2026-10-19'),
                       'reference_nav_version_id': present('synthetic-nav')}
        actual = build_estimates(action, contract, grid, observation)
        self.assertEqual({'reference_net_subscription_amount': '990.10',
                          'reference_subscription_shares': '825.08', 'fee_estimate': '9.90'}, actual['values'])
        self.assertEqual('1000.00', action['amount']['value'])

    def test_confirmed_lot_share_conservation(self):
        # 合法前置为已确认1000份且无历史卖出；冻结首步在两个情景均明确卖200份。
        action = {'action_id': 'sell-original', 'entity_id': present('fund'),
                  'side': 'sell', 'shares': present('200.00'), 'lot_trade_ids': ['buy-confirmed'],
                  'decision_at': '2026-10-20T02:00:00Z'}
        trade = {'trade_id': 'buy-confirmed', 'instrument_id': 'fund', 'side': 'buy',
                 'confirmed_shares': known('1000.00'), 'confirmation': {'state': 'confirmed'},
                 'holding_start': known({'kind': 'date', 'value': '2026-09-01', 'timezone': 'Asia/Shanghai'})}
        positions = [{'instrument_id': 'fund', 'confirmed_shares': known('1000.00')}]
        states = [{'scenario_id': scenario, 'event': 'redemption_order', 'action_id': present('sell-original'),
                   'trade_id': present('buy-confirmed'), 'share_delta': present('-200.00'),
                   'at': action['decision_at']} for scenario in ('scenario-one', 'scenario-two')]
        selected = {'allocation_dimensions': [{'side': 'sell', 'entity_id': present('fund'),
                                               'lot_rule': 'specific_confirmed_trades',
                                               'lot_trade_ids': ['buy-confirmed']}]}
        actual = resolve_lots(action, states, [trade], positions, selected, {'locked_trade_ids': []})
        self.assertEqual([('buy-confirmed', Fraction(200))], [(row['trade_id'], amount) for row, amount in actual])
        self.assertEqual(Fraction(200), sum(amount for _, amount in actual))
        self.assertEqual('200.00', action['shares']['value'])
        self.assertEqual('1000.00', positions[0]['confirmed_shares']['value'])

    def test_publication_earliest_exclusive(self):
        # 两个真实必要条件同界；集合交集的上端点只要有一个exclusive就排除此时刻。
        checks = [{'check_id': identifier, 'required': True, 'outcome': 'passed', 'boundary_kind': 'finite',
                   'valid_from': present('2026-10-20T01:00:00Z'),
                   'valid_until': present('2026-10-20T03:00:00Z'), 'upper_endpoint': endpoint}
                  for identifier, endpoint in (('condition-one', 'inclusive'), ('condition-two', 'exclusive'))]
        bound, endpoint, controlling, events, status = intersect_bounds(checks, '2026-10-20T02:00:00Z')
        self.assertEqual(present('2026-10-20T03:00:00Z'), bound)
        self.assertEqual('exclusive', endpoint)
        self.assertEqual(['condition-one', 'condition-two'], controlling)
        self.assertEqual([], events)
        self.assertEqual('eligible', status)

    def test_source_time_and_funding_condition(self):
        # SC01原日期有Asia/Shanghai时区；未来投入没有已到账事实，不增加独立现款。
        basis = self.source_ref
        raw = known({'kind': 'date', 'value': '2026-10-21', 'timezone': 'Asia/Shanghai'})
        evidence = raw_time_evidence(raw, basis)
        self.assertEqual('date', evidence['value']['precision'])
        self.assertEqual('2026-10-21T00:00:00+08:00', evidence['value']['lower_bound'])
        self.assertEqual('2026-10-21T23:59:59.999999+08:00', evidence['value']['upper_bound'])
        funding = {'available_amount': known('100.00'), 'declared_at': known('2026-10-20T01:00:00Z'),
                   'recorded_at': '2026-10-20T01:00:00Z', 'source_ref': basis,
                   'future_contributions': known([{'contribution_id': 'future', 'contribution_date': '2026-10-22',
                                                   'amount': '200.00', 'source_ref': basis}])}
        before = copy.deepcopy(funding)
        inputs = SimpleNamespace(actions=[], funding=funding, refs={'funding_plan_ref': basis},
                                 publication={'funding_review_status': 'consistent_registered_facts',
                                              'funding_confirmation_refs': [basis]},
                                 account={'pending_transactions_ref': basis},
                                 pending=[{'side': 'sell', 'pending_phase': 'settlement',
                                           'trade_id': 'earlier-sell', 'expected_at': raw}])
        rows = build_funding_conditions(inputs)
        future = next(row for row in rows if row['source_kind'] == 'future_contribution')
        self.assertEqual('pending', future['status'])
        self.assertEqual('wait_update_facts_and_replan', future['effect'])
        self.assertEqual('unknown', future['verified_funding_plan_ref']['status'])
        settlement = next(row for row in rows if row['source_kind'] == 'expected_settlement')
        self.assertEqual('date', settlement['availability']['value']['precision'])
        self.assertEqual(before, funding)


if __name__ == '__main__':
    unittest.main()
