"""SC07 验证端规划状态机；规划现金独立于任何实际账户余额。"""
from dataclasses import dataclass, field
from fractions import Fraction

from investment.contracts import require
from .policy_math import known_number, rational, round_contract


@dataclass(frozen=True)
class Purchase:
    entity_id: str
    gross: Fraction
    net: Fraction
    fee: Fraction
    paid: bool


@dataclass
class PlanningLedger:
    """准确记录模型现金、份额及应收；事件重复必须报错，不能二次入账。"""
    budget_cash: Fraction
    holdings: dict = field(default_factory=dict)
    reserved_cash: Fraction = Fraction(0)
    pending: dict = field(default_factory=dict)
    receivables: dict = field(default_factory=dict)
    locked: dict = field(default_factory=dict)
    fee_payable: Fraction = Fraction(0)
    fees_charged: Fraction = Fraction(0)
    posted: set = field(default_factory=set)

    def __post_init__(self):
        self.budget_cash = rational(self.budget_cash, 'P04')
        self.holdings = {key: rational(value, 'P04') for key, value in self.holdings.items()}
        self._nonnegative()

    def _nonnegative(self):
        amounts = [self.budget_cash, self.reserved_cash, self.fee_payable]
        amounts += list(self.holdings.values()) + list(self.receivables.values())
        require(all(value >= 0 for value in amounts), 'P08', '/state', 'A planning state cannot borrow or hold negative assets')

    def _post(self, event, identifier):
        key = event, identifier
        require(key not in self.posted, 'P04', '/event', 'The same financial event cannot be posted twice')
        self.posted.add(key)

    def available_shares(self, entity_id):
        return self.holdings.get(entity_id, Fraction(0)) - sum(
            shares for entity, shares in self.locked.values() if entity == entity_id)

    def order_purchase(self, action_id, entity_id, fee, *, paid=True):
        require(action_id not in self.pending and fee.gross > 0 and self.budget_cash >= fee.gross,
                'P08', '/subscription_order', 'A purchase requires positive gross from available planning cash')
        self._post('subscription_order', action_id)
        self.budget_cash -= fee.gross
        if paid:
            self.fees_charged += fee.fee
        else:
            self.reserved_cash += fee.gross
        self.pending[action_id] = Purchase(entity_id, fee.gross, fee.net, fee.fee, paid)
        self._nonnegative()

    def pay_purchase(self, action_id):
        require(action_id in self.pending and not self.pending[action_id].paid,
                'P08', '/subscription_payment', 'Only a reserved purchase can be paid')
        purchase = self.pending[action_id]
        require(self.reserved_cash >= purchase.gross, 'P08', '/reserved_cash', 'Reserved purchase cash is missing')
        self._post('subscription_payment', action_id)
        self.reserved_cash -= purchase.gross
        self.fees_charged += purchase.fee
        self.pending[action_id] = Purchase(purchase.entity_id, purchase.gross, purchase.net, purchase.fee, True)
        self._nonnegative()

    def confirm_purchase(self, action_id, nav, share_decimals, rounding):
        require(action_id in self.pending and self.pending[action_id].paid,
                'P08', '/subscription_confirmation', 'Confirmed shares require a previously paid purchase')
        nav = rational(nav, 'P07')
        require(nav > 0, 'P07', '/confirmation_nav', 'A documented positive original NAV is required')
        purchase = self.pending[action_id]
        shares = round_contract(purchase.net / nav, share_decimals, rounding)
        require(shares > 0, 'P05', '/confirmed_shares', 'A nonzero purchase cannot silently become zero shares')
        self._post('subscription_confirmation', action_id)
        del self.pending[action_id]
        self.holdings[purchase.entity_id] = self.holdings.get(purchase.entity_id, Fraction(0)) + shares
        self._nonnegative()
        return shares

    def order_redemption(self, action_id, entity_id, shares):
        shares = rational(shares, 'P05')
        require(action_id not in self.locked and 0 < shares <= self.available_shares(entity_id),
                'P08', '/redemption_order', 'A redemption requires confirmed unlocked shares')
        self._post('redemption_order', action_id)
        self.locked[action_id] = entity_id, shares

    def confirm_redemption(self, action_id, nav, fee):
        require(action_id in self.locked and action_id not in self.receivables,
                'P08', '/redemption_confirmation', 'A redemption must first lock its confirmed shares')
        entity, shares = self.locked[action_id]
        nav = rational(nav, 'P07')
        require(nav > 0 and fee.gross == shares * nav, 'P07', '/redemption_gross',
                'Redemption gross must use its original confirmation NAV and exact locked shares')
        self._post('redemption_confirmation', action_id)
        del self.locked[action_id]
        self.holdings[entity] -= shares
        self.receivables[action_id] = fee.net
        self.fees_charged += fee.fee
        self._nonnegative()

    def settle(self, receipt_id):
        require(receipt_id in self.receivables, 'P08', '/settlement', 'There is no unsettled net receivable')
        self._post('settlement', receipt_id)
        self.budget_cash += self.receivables.pop(receipt_id)
        self._nonnegative()

    def contribute(self, contribution_id, amount):
        amount = rational(amount, 'P04')
        require(amount > 0, 'P04', '/contribution', 'A documented contribution must be positive')
        self._post('contribution', contribution_id)
        self.budget_cash += amount

    def wealth(self, original_navs):
        position_value = Fraction(0)
        for entity, shares in self.holdings.items():
            if shares == 0:
                continue
            require(entity in original_navs, 'P04', '/position_value', 'Original NAV is missing for a registered asset', 'missing_evidence')
            nav = rational(original_navs[entity], 'P04')
            require(nav >= 0, 'P04', '/nav', 'Original NAV cannot be negative')
            position_value += shares * nav
        pending_asset = sum((purchase.net for purchase in self.pending.values() if purchase.paid), Fraction(0))
        receivable = sum(self.receivables.values(), Fraction(0))
        wealth = self.budget_cash + self.reserved_cash + position_value + pending_asset + receivable - self.fee_payable
        return {'budget_cash': self.budget_cash, 'reserved_cash': self.reserved_cash,
                'position_value': position_value, 'pending_asset': pending_asset,
                'receivable': receivable, 'fee_payable': self.fee_payable, 'wealth': wealth}


def registered_start(trades, positions, pending, funding, entity_by_instrument):
    """只消费本轮SC01三表；历史已到账卖款不再加独立规划额度。"""
    cash = known_number(funding['available_amount'], 'P04', '/funding_plan/available_amount')
    ledger = PlanningLedger(cash)
    trades_by_id = {row['trade_id']: row for row in trades}
    require(len(trades_by_id) == len(trades), 'P01', '/trade_id', 'The bound trade table has duplicate identifiers')
    for position in positions:
        instrument = position['instrument_id']
        require(instrument in entity_by_instrument, 'P04', '/positions',
                'The registered instrument has no authoritative entity identity', 'missing_evidence')
        entity = entity_by_instrument[instrument]
        shares = known_number(position['confirmed_shares'], 'P04', '/positions/confirmed_shares')
        ledger.holdings[entity] = ledger.holdings.get(entity, Fraction(0)) + shares
    seen = set()
    for row in pending:
        trade_id = row['trade_id']
        require(trade_id not in seen and trade_id in trades_by_id, 'P01', '/pending', 'Pending transaction binding is not unique')
        seen.add(trade_id)
        trade = trades_by_id[trade_id]
        instrument = trade['instrument_id']
        require(instrument in entity_by_instrument, 'P04', '/pending/entity',
                'A pending instrument has no authoritative entity identity', 'missing_evidence')
        entity = entity_by_instrument[instrument]
        if row['side'] == 'buy':
            gross = known_number(trade['amount'], 'P04', '/pending_buy/amount')
            fee = known_number(trade['fee_amount'], 'P07', '/pending_buy/fee_amount')
            require(0 <= fee <= gross, 'P07', '/pending_buy/fee', 'Recorded purchase fee is outside actual gross')
            ledger.pending[trade_id] = Purchase(entity, gross, gross - fee, fee, True)
        elif row['pending_phase'] == 'settlement':
            ledger.receivables[trade_id] = known_number(trade['amount'], 'P04', '/pending_sell/net_amount')
        else:
            shares = known_number(trade['requested_shares'], 'P08', '/pending_sell/requested_shares')
            require(0 < shares <= ledger.available_shares(entity), 'P08', '/pending_sell/shares',
                    'Pending redemption must retain and lock existing confirmed shares')
            ledger.locked[trade_id] = entity, shares
    ledger._nonnegative()
    return ledger
