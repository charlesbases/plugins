"""SC07 验证端有理数计算，不调用生产端的评价或选择算法。"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from investment.contracts import ContractError, require


def rational(value, rule='P11', path='/number'):
    """将有限十进制原值变成精确有理数，拒绝二进制浮点和布尔值。"""
    require(type(value) in (str, int, Decimal, Fraction), rule, path,
            'An exact finite number is required')
    if isinstance(value, Fraction):
        return value
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ContractError(rule, path, 'Invalid exact number') from exc
    require(decimal.is_finite(), rule, path, 'A finite number is required')
    return Fraction(decimal)


def known_number(value, rule='P04', path='/number'):
    require(value['status'] == 'known', rule, path,
            'Required source value is unknown', 'missing_evidence')
    return rational(value['value'], rule, path)


def round_contract(value, decimal_places, rounding):
    """按合同十进制精度舍入，整个过程不经浮点或上下文截断。"""
    require(type(decimal_places) is int and decimal_places >= 0, 'P07', '/precision',
            'Contract decimal places must be a nonnegative integer')
    require(rounding in ('floor', 'ceiling', 'half_up', 'half_even'), 'P07', '/rounding',
            'The contract rounding algorithm is not executable', 'missing_evidence')
    scale = 10 ** decimal_places
    scaled = rational(value, 'P07') * scale
    lower, remainder = divmod(scaled.numerator, scaled.denominator)
    if rounding == 'floor':
        result = lower
    elif rounding == 'ceiling':
        result = lower + (remainder != 0)
    else:
        twice = 2 * remainder
        result = lower + (twice > scaled.denominator)
        if twice == scaled.denominator:
            result = lower + (scaled >= 0 if rounding == 'half_up' else lower % 2 != 0)
    return Fraction(result, scale)


def quantize_step(value, step, rounding):
    step = rational(step, 'P05')
    require(step > 0, 'P05', '/step', 'A positive documented step is required')
    return round_contract(rational(value, 'P05') / step, 0, rounding) * step


@dataclass(frozen=True)
class FeeCalculation:
    """合同费用和相应净资产额；tier_id保留准确原费阶。"""
    gross: Fraction
    net: Fraction
    fee: Fraction
    tier_id: str


def fee_for_tier(gross, tier, money_decimals, rounding):
    """对已独立匹配的准确费阶核申购、赎回、固定费及NAV内费用。"""
    gross = rational(gross, 'P07')
    require(gross >= 0, 'P07', '/gross', 'Gross amount must be nonnegative')
    calculation = tier['calculation']
    require(calculation != 'external_subscription_ratio' or tier['charge'] == 'subscription',
            'P07', '/charge', 'External subscription fees require the subscription charge')
    require(calculation != 'redemption_gross_ratio' or tier['charge'] == 'redemption',
            'P07', '/charge', 'Redemption ratios require the redemption charge')
    if calculation == 'included_in_nav':
        fee = Fraction(0)
    elif calculation in ('external_subscription_ratio', 'redemption_gross_ratio'):
        rate = known_number(tier['rate'], 'P07', '/rate')
        require(rate >= 0, 'P07', '/rate', 'A fee rate cannot be negative')
        require(calculation != 'redemption_gross_ratio' or rate <= 1,
                'P07', '/rate', 'A redemption ratio cannot exceed gross proceeds')
        raw = gross - gross / (1 + rate) if calculation == 'external_subscription_ratio' else gross * rate
        fee = round_contract(raw, money_decimals, rounding)
    elif calculation == 'fixed_per_trade':
        require(tier['currency']['status'] == 'known' and tier['currency']['value'] == 'CNY',
                'P07', '/currency', 'Fixed fees require the exact CNY contract currency', 'missing_evidence')
        fee = round_contract(known_number(tier['fixed_amount'], 'P07', '/fixed_amount'), money_decimals, rounding)
    else:
        require(False, 'P07', '/calculation', 'No registered executable documented formula is available', 'missing_evidence')
    require(0 <= fee <= gross, 'P07', '/fee', 'Fee is outside gross proceeds')
    net = gross - fee
    if tier['charge'] == 'redemption':
        # 净款按原合同货币精度舍入；舍入差不冒充新增费用或约束容差。
        net = round_contract(net, money_decimals, rounding)
    return FeeCalculation(gross, net, fee, tier['tier_id'])


def applicable_tier(tiers, charge, amount, holding_days):
    """依据原合同半开金额及持有日区间匹配，不把unknown上界当无穷。"""
    amount = rational(amount, 'P05')
    require(type(holding_days) is int and holding_days >= 0, 'P05', '/holding_days',
            'Holding days require an independently supported integer')
    matches = []
    for tier in tiers:
        if tier['charge'] != charge:
            continue
        upper = tier['amount_upper']
        days_upper = tier['holding_days_upper']
        if amount < rational(tier['amount_lower'], 'P05') or holding_days < tier['holding_days_lower']:
            continue
        if upper['status'] == 'known' and amount >= rational(upper['value'], 'P05'):
            continue
        if days_upper['status'] == 'known' and holding_days >= days_upper['value']:
            continue
        require(upper['status'] != 'unknown' and days_upper['status'] != 'unknown',
                'P05', '/fee_tier', 'Fee interval boundary is unknown', 'missing_evidence')
        in_amount = amount >= rational(tier['amount_lower'], 'P05') and (
            upper['status'] == 'not_applicable' or amount < rational(upper['value'], 'P05'))
        in_days = holding_days >= tier['holding_days_lower'] and (
            days_upper['status'] == 'not_applicable' or holding_days < days_upper['value'])
        if in_amount and in_days:
            matches.append(tier)
    require(len(matches) == 1, 'P05', '/fee_tier',
            'An action requires exactly one supported applicable fee tier', 'missing_evidence')
    return matches[0]


@dataclass(frozen=True)
class TailStatistics:
    """有限损失分布的精确分位及边界原子尾均值，单位沿输入CNY。"""
    expected_wealth: Fraction
    var: Fraction
    cvar: Fraction
    losses: tuple


def tail_statistics(terminal_wealth, probabilities, reference_wealth, beta):
    wealth = tuple(rational(item) for item in terminal_wealth)
    probabilities = tuple(rational(item) for item in probabilities)
    reference_wealth, beta = rational(reference_wealth), rational(beta)
    require(bool(wealth) and len(wealth) == len(probabilities), 'P11', '/scenarios',
            'Wealth and exact probabilities must cover the same nonempty scenario set')
    require(all(item > 0 for item in probabilities) and sum(probabilities) == 1,
            'P11', '/probabilities', 'Scenario probabilities must be positive and sum exactly to one')
    require(0 < beta < 1, 'P11', '/beta', 'CVaR beta must be strictly between zero and one')
    losses = tuple(reference_wealth - value for value in wealth)
    cumulative = Fraction(0)
    var = None
    for loss, probability in sorted(zip(losses, probabilities)):
        cumulative += probability
        if cumulative >= beta:
            var = loss
            break
    remaining = 1 - beta
    tail_total = Fraction(0)
    for loss, probability in sorted(zip(losses, probabilities), reverse=True):
        used = min(remaining, probability)
        tail_total += used * loss
        remaining -= used
        if remaining == 0:
            break
    require(remaining == 0, 'P11', '/tail', 'The required tail probability was not covered')
    return TailStatistics(sum(p * w for p, w in zip(probabilities, wealth)), var,
                          tail_total / (1 - beta), losses)


@dataclass(frozen=True)
class FiniteEvaluation:
    """一个已独立完成政策的准确目标、风险可行性与动作并列键。"""
    policy_id: str
    objective: Fraction
    feasible: bool
    action_ids: tuple = ()


@dataclass(frozen=True)
class FiniteComparison:
    selected: FiniteEvaluation | None
    evaluated_ids: tuple
    uncomputed_ids: tuple
    exact_declared_space: bool


def compare_finite(declared_ids, evaluations):
    """仅对声明有限集合独立排序；缺政策不被删除或改称全局最优。"""
    declared = tuple(sorted(declared_ids))
    evaluations = tuple(evaluations)
    ids = tuple(sorted(item.policy_id for item in evaluations))
    require(len(declared) == len(set(declared)) and len(ids) == len(set(ids))
            and set(ids) <= set(declared), 'P14', '/policy_ids', 'Duplicate or undeclared policy evaluation')
    feasible = [item for item in evaluations if item.feasible]
    selected = min(feasible, key=lambda item: (-rational(item.objective, 'P14'), item.policy_id,
                                             tuple(sorted(item.action_ids)))) if feasible else None
    uncomputed = tuple(sorted(set(declared) - set(ids)))
    return FiniteComparison(selected, ids, uncomputed, not uncomputed)
