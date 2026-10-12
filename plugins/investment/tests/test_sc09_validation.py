"""SC09 独立基础校准；不构造封存、Host 或完整 selector 成功。"""
import unittest
from fractions import Fraction

from investment.chains.recommendation.contracts import Issue
from investment.chains.recommendation.deterministic import exact_instruction, scenario_lots
from investment.chains.recommendation.finance import round_fraction, subscription, redemption
from investment.chains.recommendation.temporal import intersect
from investment.chains.recommendation.semantic_review import _pointer


def k(value):
    return {'status': 'known', 'value': value, 'reason': None}


class ExactProjectionCalibration(unittest.TestCase):
    """合法原指令和明示费率/精度的数学投影，预期先于实现固定。"""

    def test_payment_and_share_instruction_preserve_original_text(self):
        self.assertEqual(exact_instruction({'side': 'buy', 'amount': k('1000.00')}), '1000.00')
        self.assertEqual(exact_instruction({'side': 'sell', 'shares': k('1000.000')}), '1000.000')

    def test_explicit_external_subscription_contract(self):
        # 明示实付含费 1010 CNY、费率 1%、NAV 1.25，净额 1000、费 10、参考份额 800。
        self.assertEqual(subscription('1010', '1.25', '0.01'),
                         (Fraction(1000), Fraction(10), Fraction(800)))

    def test_confirmed_lot_redemption_gross_fee_net(self):
        # 已确认买入的 1000 份，明示参考 NAV=1.2、赎回费率=1%；不声称实际成交/到账。
        self.assertEqual(redemption('1000', '1.2', '0.01'),
                         (Fraction(1200), Fraction(12), Fraction(1188)))

    def test_source_final_rounding_only(self):
        self.assertEqual(round_fraction(Fraction(1005, 1000), 2, 'half_even'), Fraction(1))
        self.assertEqual(round_fraction(Fraction(1005, 1000), 2, 'half_up'), Fraction(101, 100))

    def test_frozen_current_redemption_all_scenarios_same_lot(self):
        # 两个合法情景均从已确认买入 lot-1 赎回 400 份；首步必须相同。
        rows = [{'scenario_id': sid, 'action_id': k('sell-1'), 'trade_id': k('lot-1'),
                 'event': 'redemption_confirmation', 'share_delta': k('-400')}
                for sid in ('scenario-1', 'scenario-2')]
        self.assertEqual(scenario_lots(rows, 'sell-1'), {'lot-1': Fraction(400)})


class SourceLocatorCalibration(unittest.TestCase):
    """实际 JSON 原文投影的定位校准，不创建 Host 或上游凭证。"""

    def test_exact_source_pointer_rejects_negative_and_absent_array_location(self):
        source = {'rows': [{'source': 'registered original text'}]}
        self.assertEqual(_pointer(source, '/rows/0/source'), 'registered original text')
        with self.assertRaises(Issue):
            _pointer(source, '/rows/-1/source')
        with self.assertRaises(Issue):
            _pointer(source, '/rows/1/source')


class PublicationIntervalCalibration(unittest.TestCase):
    """源边界已明确的数学交集，不冒充发布事实核验。"""

    def test_tied_earliest_exclusive_and_all_controls(self):
        rows = [('a', '2026-10-20T00:00:00Z', '2026-10-20T08:00:00Z', 'inclusive'),
                ('b', '2026-10-20T01:00:00Z', '2026-10-20T08:00:00Z', 'exclusive'),
                ('c', '2026-10-20T00:00:00Z', '2026-10-20T09:00:00Z', 'exclusive')]
        end, endpoint, controls = intersect(rows, '2026-10-20T07:00:00Z')
        self.assertEqual((end, endpoint, controls), ('2026-10-20T08:00:00Z', 'exclusive', ['a', 'b']))
        with self.assertRaises(Issue):
            intersect(rows, '2026-10-20T08:00:00Z')

    def test_no_finite_boundary_does_not_invent_ttl(self):
        with self.assertRaises(Issue) as caught:
            intersect([], '2026-10-20T08:00:00Z')
        self.assertEqual(caught.exception.verdict, 'insufficient_evidence')


if __name__ == '__main__':
    unittest.main()
