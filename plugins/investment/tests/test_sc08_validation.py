"""仅校准SC08独立算术基础；不构造上游封存或声称selector通过。"""
from fractions import Fraction
import unittest

from jsonschema import Draft202012Validator

from investment.research.contracts import ResearchSchemas

from investment.chains.qualification.validation_finance import (
    reconciliation_differences, redemption_confirmation_expected, settlement_expected,
)
from investment.chains.qualification.validation_numbers import rounded_decimal
from investment.chains.qualification.validation_statistics import (
    empirical_cvar, losses, paired_deltas, paired_mean, synchronized_mbb_vectors,
)


def _state(*, budget_cash, receivable, position_value, wealth, shares, locked, market):
    """synthetic_fixture数值外壳；字面预期由契约手算预先确定。"""
    def known(value):
        return {'status': 'known', 'value': value, 'reason': None}

    return {
        'budget_cash': known(budget_cash), 'reserved_cash': known('0'),
        'pending_asset': known('0'), 'receivable': known(receivable),
        'fee_payable': known('0'), 'position_value': known(position_value),
        'wealth': known(wealth), 'lots': [{
            'lot_id': 'fixture-lot',
            'entity_id': {'status': 'known', 'value': 'fixture-fund', 'reason': ''},
            'share_class_id': {'status': 'known', 'value': 'fixture-class', 'reason': ''},
            'origin_trade_id': {'status': 'known', 'value': 'fixture-buy', 'reason': ''},
            'acquired_at': {'status': 'known', 'value': '2026-10-01T00:00:00Z', 'reason': ''},
            'confirmed_shares': known(shares), 'locked_shares': known(locked),
            'market_value': known(market),
        }],
    }


class SC08ValidationFoundationTests(unittest.TestCase):
    def test_independent_ledger_redemption_and_arrival(self):
        schemas = ResearchSchemas(('SC03', 'SC04', 'SC05', 'SC06', 'SC07', 'SC08'))
        state_schema = Draft202012Validator(
            {'$ref': 'urn:investment:spec:v1:sc08#/$defs/FinancialState'},
            registry=schemas.resources, format_checker=schemas.format_checker,
        )
        # 合法基础前提：10份已确认且全部为本笔锁定，账面100，明确同规则源舍入。
        original = _state(budget_cash='0', receivable='0', position_value='100',
                          wealth='100', shares='10', locked='10', market='100')
        state_schema.validate(original)
        confirmed, amounts = redemption_confirmation_expected(
            original, lot_id='fixture-lot', quantity='10', nav='10', fee_rate='0.01',
            removed_market_value='100', decimal_places=2, rounding='half_up',
        )
        self.assertEqual(amounts, {'gross_amount': Fraction(100), 'fee_amount': Fraction(1),
                                  'net_amount': Fraction(99), 'quantity': Fraction(10),
                                  'nav': Fraction(10)})
        expected_confirmation = _state(
            budget_cash='0', receivable='99', position_value='0', wealth='99',
            shares='0', locked='0', market='0',
        )
        for state in (expected_confirmation, confirmed):
            state_schema.validate(state)
        self.assertEqual(confirmed, expected_confirmation)
        differences = reconciliation_differences(expected_confirmation, confirmed)
        self.assertEqual(set(differences), {
            'budget_cash', 'reserved_cash', 'pending_asset', 'receivable', 'fee_payable',
            'position_value', 'wealth', 'lots/fixture-lot/confirmed_shares',
            'lots/fixture-lot/locked_shares', 'lots/fixture-lot/market_value',
        })
        self.assertEqual({path: row['difference'] for path, row in differences.items()},
                         dict.fromkeys(differences, Fraction(0)))
        self.assertEqual(differences['receivable']['expected'], Fraction(99))
        self.assertEqual(differences['lots/fixture-lot/confirmed_shares']['unit'], 'share')
        self.assertEqual(differences['wealth']['unit'], 'CNY')
        self.assertTrue(all(row['tolerance'] == 0 for row in differences.values()))
        # 本步已明确确认净额99到账，仅对应此笔应收转现金；不产生到账事实凭证。
        arrived = settlement_expected(confirmed, confirmed_net='99')
        expected_arrival = _state(
            budget_cash='99', receivable='0', position_value='0', wealth='99',
            shares='0', locked='0', market='0',
        )
        for state in (expected_arrival, arrived):
            state_schema.validate(state)
        self.assertEqual(arrived, expected_arrival)
        self.assertTrue(all(row['difference'] == 0 for row in
                            reconciliation_differences(expected_arrival, arrived).values()))
        self.assertEqual(original, _state(
            budget_cash='0', receivable='0', position_value='100', wealth='100',
            shares='10', locked='10', market='100',
        ))

    def test_paired_mean_and_atomic_cvar(self):
        # 三origin只校算术，不证明成熟性、来源、独立开放或科学充分。
        policy, baseline = ('110', '90', '105'), ('100', '95', '100')
        self.assertEqual(paired_deltas(policy, baseline),
                         (Fraction(10), Fraction(-5), Fraction(5)))
        mean = paired_mean(policy, baseline)
        self.assertEqual(mean, Fraction(10, 3))
        self.assertEqual(str(rounded_decimal(mean, places=2, rounding='half_even')), '3.33')
        self.assertEqual(str(rounded_decimal(mean, places=2, rounding='floor')), '3.33')
        self.assertEqual(str(rounded_decimal(mean, places=2, rounding='ceiling')), '3.34')
        policy_losses = losses('100', policy)
        self.assertEqual(policy_losses, (Fraction(-10), Fraction(10), Fraction(-5)))
        # 最坏一半质量：10×1/3 + (-5)×1/6，除以1/2，独立预期5。
        self.assertEqual(empirical_cvar(policy_losses, beta='0.5',
                                       weights=(Fraction(1, 3),) * 3), Fraction(5))
        # 已给定合法重叠块，仅核算[0,1,0]；不宣称任何正式seed会生成它。
        sampled = synchronized_mbb_vectors(
            (policy, baseline, policy_losses), block_length=2,
            block_starts=(0, 0), origin_indices=(0, 1, 0),
        )
        self.assertEqual(sampled, ((Fraction(110), Fraction(90), Fraction(110)),
                                  (Fraction(100), Fraction(95), Fraction(100)),
                                  (Fraction(-10), Fraction(10), Fraction(-10))))
        self.assertEqual(paired_mean(sampled[0], sampled[1]), Fraction(5))
