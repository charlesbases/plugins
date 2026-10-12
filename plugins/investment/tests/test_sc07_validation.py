"""SC07 独立基础核算；不伪装成四selector或process/validate流程已运行。"""
from fractions import Fraction as F
import unittest

from investment.contracts import ContractError
from investment.chains.portfolio_selection.finance import PlanningLedger, registered_start
from investment.chains.portfolio_selection.policy_math import (
    FeeCalculation, FiniteEvaluation, applicable_tier, compare_finite,
    fee_for_tier, quantize_step, rational, round_contract, tail_statistics,
)


def known(value):
    return {'status': 'known', 'value': value, 'reason': None}


def tier(calculation, rate='0', identifier='fee'):
    # 仅基础计算器所消费的内部投影，不称已验SC05合同或上游fixture。
    return {'tier_id': identifier, 'charge': 'subscription' if calculation == 'external_subscription_ratio' else 'redemption',
            'calculation': calculation, 'rate': known(rate), 'fixed_amount': known('1'),
            'currency': known('CNY'), 'amount_lower': '0',
            'amount_upper': {'status': 'not_applicable', 'value': None, 'reason': 'unbounded documented premise'},
            'holding_days_lower': 0,
            'holding_days_upper': {'status': 'not_applicable', 'value': None, 'reason': 'unbounded documented premise'}}


class FinancialFoundations(unittest.TestCase):
    """准入为明确合同/价格假设下的基础函数核算，不依赖虚假seal。"""

    def test_bought_then_redeemed_cash_unavailable_until_settlement(self):
        # 前置：规划额100，先以NAV10、零申购费实际模型买入10份。
        ledger = PlanningLedger(F(100))
        ledger.order_purchase('buy', 'fund', FeeCalculation(F(100), F(100), F(0), 'documented-zero'))
        self.assertEqual(ledger.confirm_purchase('buy', '10', 2, 'half_up'), 10)
        ledger.order_redemption('sell', 'fund', '10')
        fee = fee_for_tier('100', tier('redemption_gross_ratio', '0.01'), 2, 'half_up')
        ledger.confirm_redemption('sell', '10', fee)
        self.assertEqual(ledger.holdings['fund'], 0)
        self.assertEqual(ledger.receivables['sell'], 99)
        self.assertEqual(ledger.budget_cash, 0)
        self.assertEqual(ledger.fees_charged, 1)
        self.assertEqual(ledger.wealth({})['wealth'], 99)
        with self.assertRaises(ContractError):
            ledger.order_purchase('too-early', 'fund', FeeCalculation(F(99), F(99), F(0), 'documented-zero'))
        ledger.settle('sell')
        self.assertEqual(ledger.budget_cash, 99)
        self.assertEqual(ledger.receivables, {})
        self.assertEqual(ledger.holdings['fund'], 0)
        self.assertEqual(ledger.fees_charged, 1)
        with self.assertRaises(ContractError):
            ledger.settle('sell')

    def test_reserved_purchase_is_not_also_pending_asset(self):
        # 前置：合同允许先冻结100，固定费用1；付款与确认是不同事件。
        ledger = PlanningLedger(F(100))
        ledger.order_purchase('buy', 'fund', FeeCalculation(F(100), F(99), F(1), 'fixed'), paid=False)
        self.assertEqual(ledger.wealth({}), {'budget_cash': 0, 'reserved_cash': 100,
            'position_value': 0, 'pending_asset': 0, 'receivable': 0, 'fee_payable': 0, 'wealth': 100})
        ledger.pay_purchase('buy')
        self.assertEqual(ledger.wealth({})['pending_asset'], 99)
        self.assertEqual(ledger.wealth({})['reserved_cash'], 0)
        self.assertEqual(ledger.wealth({})['wealth'], 99)
        self.assertEqual(ledger.confirm_purchase('buy', '10', 2, 'half_even'), F(99, 10))
        self.assertEqual(ledger.wealth({'fund': '10'})['wealth'], 99)
        self.assertEqual(ledger.fees_charged, 1)

    def test_independent_budget_does_not_readd_historical_proceeds(self):
        # 原三表内部投影：已买10份并已确认全部卖出，净应收99仍在途。
        buy = {'trade_id': 'buy', 'instrument_id': 'instrument', 'amount': known('100'),
               'fee_amount': known('0'), 'requested_shares': known('10')}
        sell = {'trade_id': 'sell', 'instrument_id': 'instrument', 'amount': known('99'),
                'fee_amount': known('1'), 'requested_shares': known('10')}
        positions = [{'instrument_id': 'instrument', 'confirmed_shares': known('0')}]
        pending = [{'trade_id': 'sell', 'side': 'sell', 'pending_phase': 'settlement'}]
        funding = {'available_amount': known('40')}
        ledger = registered_start([buy, sell], positions, pending, funding, {'instrument': 'fund'})
        self.assertEqual(ledger.budget_cash, 40)
        self.assertEqual(ledger.wealth({})['wealth'], 139)
        ledger.settle('sell')
        self.assertEqual(ledger.budget_cash, 139)
        self.assertEqual(ledger.holdings['fund'], 0)
        settled_start = registered_start([buy, sell], positions, [], funding, {'instrument': 'fund'})
        self.assertEqual(settled_start.budget_cash, 40)
        self.assertEqual(settled_start.wealth({})['wealth'], 40)

    def test_fees_precision_and_embedded_costs(self):
        # 原公式100/(1+.01)=10000/101；合同费用按2位half_up后为.99。
        self.assertEqual(F(100) / (1 + F(1, 100)), F(10000, 101))
        external = fee_for_tier('100', tier('external_subscription_ratio', '0.01'), 2, 'half_up')
        self.assertEqual((external.net, external.fee), (F(9901, 100), F(99, 100)))
        embedded_tier = tier('included_in_nav')
        embedded_tier['charge'] = 'management'
        embedded = fee_for_tier('100', embedded_tier, 2, 'half_up')
        self.assertEqual((embedded.net, embedded.fee), (100, 0))
        # 合法多位NAV边界：先以NAV1买入1.23份，再按NAV1.2345赎回。
        # 货币2位half_up；原始gross1.518435，fee.02，最终净应收1.50。
        ledger = PlanningLedger(F(123, 100))
        ledger.order_purchase('fractional-buy', 'fund', FeeCalculation(F(123, 100), F(123, 100), F(0), 'documented-zero'))
        ledger.confirm_purchase('fractional-buy', '1', 2, 'half_up')
        before = ledger.wealth({'fund': '1.2345'})['wealth']
        ledger.order_redemption('fractional-sell', 'fund', '1.23')
        redemption = fee_for_tier(F(123, 100) * F(12345, 10000), tier('redemption_gross_ratio', '0.01'), 2, 'half_up')
        self.assertEqual((redemption.net, redemption.fee), (F(3, 2), F(1, 50)))
        adjustment = redemption.net + redemption.fee - redemption.gross
        self.assertEqual(adjustment, F(313, 200000))
        ledger.confirm_redemption('fractional-sell', '1.2345', redemption)
        self.assertEqual(ledger.receivables['fractional-sell'], F(3, 2))
        self.assertEqual(ledger.wealth({})['wealth'], before - F(1, 50) + adjustment)
        ledger.settle('fractional-sell')
        self.assertEqual((ledger.budget_cash, ledger.holdings['fund'], ledger.fees_charged), (F(3, 2), 0, F(1, 50)))
        self.assertEqual(round_contract('-1.225', 2, 'half_up'), F(-123, 100))
        self.assertEqual(round_contract('-1.225', 2, 'half_even'), F(-122, 100))
        self.assertEqual(quantize_step('12.39', '0.1', 'floor'), F(123, 10))
        with self.assertRaises(ContractError):
            rational(.1)
        with self.assertRaises(ContractError):
            rational(True)

    def test_original_half_open_fee_boundary_and_unknown(self):
        lower = tier('redemption_gross_ratio', '0.01', 'below-100')
        lower['amount_upper'] = known('100')
        upper = tier('redemption_gross_ratio', '0.005', 'from-100')
        upper['amount_lower'] = '100'
        self.assertEqual(applicable_tier([lower, upper], 'redemption', '100', 1)['tier_id'], 'from-100')
        upper['amount_upper'] = {'status': 'unknown', 'value': None, 'reason': 'source boundary unavailable'}
        with self.assertRaises(ContractError) as error:
            applicable_tier([lower, upper], 'redemption', '100', 1)
        self.assertEqual(error.exception.code, 'missing_evidence')


class MathematicalFoundations(unittest.TestCase):
    def test_atomic_cvar_and_negative_losses(self):
        # 原预期：等概率10/30损失尾均值30；原子.2/.5/.3的尾均值22。
        simple = tail_statistics(['90', '70'], [F(1, 2)] * 2, '100', '0.5')
        self.assertEqual((simple.var, simple.cvar), (10, 30))
        atom = tail_statistics(['100'] * 2 + ['90'] * 5 + ['70'] * 3, [F(1, 10)] * 10, '100', '0.5')
        self.assertEqual((atom.var, atom.cvar), (10, 22))
        gains = tail_statistics(['110', '120'], [F(1, 2)] * 2, '100', '0.5')
        self.assertEqual(gains.losses, (-10, -20))
        self.assertEqual(gains.cvar, -10)
        # 非终止小数保留内存有理数；本测试不授予任何序列化新语义。
        thirds = tail_statistics(['100', '100', '101'], [F(1, 3)] * 3, '100', '0.5')
        self.assertEqual(thirds.expected_wealth, F(301, 3))

    def test_independent_legal_finite_wealth_and_exact_winner(self):
        # 明确模型前置：同起点100、外扣申购费率1%、步长1、份额2位、NAV10。
        # 独立手算：现金100；投入50→两情景99.5/109.4；投入100→99/118.8。
        expected = {'cash': (F(100), F(100)), 'half': (F(199, 2), F(547, 5)),
                    'all': (F(99), F(594, 5))}
        evaluations = []
        for policy_id, gross in [('cash', 0), ('half', 50), ('all', 100)]:
            ledger = PlanningLedger(F(100))
            if gross:
                # 费额先按2位舍入，确认份额再按2位舍入；独立预期已在上表给定。
                fee = fee_for_tier(str(gross), tier('external_subscription_ratio', '0.01'), 2, 'half_up')
                ledger.order_purchase('buy', 'fund', fee)
                ledger.confirm_purchase('buy', '10', 2, 'half_up')
            actual = tuple(ledger.wealth({'fund': nav})['wealth'] for nav in ('10', '12'))
            self.assertEqual(actual, expected[policy_id])
            risk = tail_statistics(actual, [F(1, 2)] * 2, '100', '0.5')
            evaluations.append(FiniteEvaluation(policy_id, risk.expected_wealth, risk.cvar <= 1))
        complete = compare_finite(['cash', 'half', 'all'], evaluations)
        self.assertTrue(complete.exact_declared_space)
        self.assertEqual((complete.selected.policy_id, complete.selected.objective), ('all', F(1089, 10)))
        limited = compare_finite(['cash', 'half', 'all', 'unknown-member-policy'], evaluations[:2])
        self.assertFalse(limited.exact_declared_space)
        self.assertEqual(limited.uncomputed_ids, ('all', 'unknown-member-policy'))

    def test_exact_tie_and_declared_scope_are_preserved(self):
        tied = [FiniteEvaluation('z', F(100), True), FiniteEvaluation('a', F(100), True)]
        self.assertEqual(compare_finite(['z', 'a'], tied).selected.policy_id, 'a')
        exact = [FiniteEvaluation('a', F(1), True), FiniteEvaluation('z', F(10**20 + 1, 10**20), True)]
        self.assertEqual(compare_finite(['a', 'z'], exact).selected.policy_id, 'z')


if __name__ == '__main__':
    unittest.main()
