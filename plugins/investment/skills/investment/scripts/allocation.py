"""Source cash/share actions; whole-path MPC alone ranks investments."""
import copy
import itertools
import math
from decimal import Decimal, ROUND_CEILING

import fee_contract as fees
import ledger
import trading_calendar
from contracts import ContractError, fingerprint, instant, require

MENU_RULE = "source_precision_up_to_16_else_endpoints_quarters_fee_boundaries_v2"
ZERO = Decimal(0)


def _cvar(losses, probabilities, alpha):
    remaining, total = alpha, 0.0
    for loss, weight in sorted(zip(losses, probabilities), reverse=True):
        used = min(remaining, weight)
        total += used * loss
        remaining -= used
        if remaining <= 1e-12:
            break
    return total / alpha


def funded_context(context, funding):
    """Proposed capital is separate from the actual account."""
    research = copy.deepcopy(context)
    amount = fees.number(funding)
    require(amount >= 0, "Nonnegative proposed funding required")
    for key in ("cash", "available_cash", "equity"):
        research["snapshot"][key] = ledger.decimal(Decimal(research["snapshot"][key])+amount)
    risk = research["risk_state"]
    principal = Decimal(risk["net_principal"])+amount
    floor = principal*(1-Decimal(risk["loss_tolerance"]))
    risk.update(net_principal=ledger.decimal(principal), principal_floor=ledger.decimal(floor),
                remaining_loss_budget=ledger.decimal(Decimal(research["snapshot"]["equity"])-floor))
    research["context_hash"] = fingerprint({key:value for key,value in research.items() if key!="context_hash"})
    return research


def _split_redemption(lots, quantity, method):
    remaining, sells = quantity, []
    ordered = sorted(lots, key=lambda row: (instant(row["acquired_at"]), row["lot_id"]), reverse=method == "lifo")
    for lot in ordered:
        selected = min(remaining, fees.number(lot["shares"])-fees.number(lot["reserved_shares"]))
        if selected > 0:
            sells.append({"lot_id": lot["lot_id"], "shares": ledger.decimal(selected)})
            remaining -= selected
        if remaining == 0:
            break
    require(remaining == 0, "Redemption exceeds available source lots")
    return sells


def _action_supported(terms, side):
    evidence = terms.get("action_evidence")
    if evidence is None:
        return True
    row = evidence.get(side, {})
    return row.get("status") == "source_supported" and row.get("missing_fields") == []


def validate_redemption_fee_scope(terms, lot_count):
    if lot_count > 1:
        require(terms.get("redemption_fee_application") == "per_lot" and terms.get("redemption_fee_application_source")
                and terms.get("redemption_rounding_application") == "per_lot" and terms.get("redemption_rounding_application_source"),
                "Original source multi-lot redemption fee application and rounding scope required; per-request aggregation is unsupported")


def validate_source_action(context, action, *, funding=0):
    """Enforce exact units, aggregate minima and source lot allocation.

    Called against the current snapshot or a modeled future state. A single
    lot or redemption of every available share does not depend on lot order.
    """
    require(type(action) is dict and set(action) == {"buys", "sells"}, "Canonical cash/share action required")
    require(type(action["buys"]) is list and type(action["sells"]) is list, "Canonical action legs required")
    snapshot = context["snapshot"]
    assets = {row["code"]: row for row in context["model_request"]["assets"]}
    lots = {row["lot_id"]: row for row in snapshot["positions"]}
    require(len(lots) == len(snapshot["positions"]), "Unique source lot identities required")
    sold, seen = {}, set()
    for trade in action["sells"]:
        require(type(trade) is dict and set(trade) == {"lot_id", "shares"}, "Sale requires source lot and shares")
        identity = trade["lot_id"]
        require(identity in lots and identity not in seen, "Unknown or repeated sale lot")
        seen.add(identity)
        lot = lots[identity]
        code, terms = lot["code"], context["fee_contracts"][lot["code"]]
        quantity, step = fees.number(trade["shares"]), fees.number(terms["trade_precision"]["share_step"])
        available = fees.number(lot["shares"])-fees.number(lot["reserved_shares"])
        require(quantity > 0 and quantity == fees.floor(quantity, step), "Sale differs from source precision")
        require(assets[code]["sellable"] and terms["sellable"] and quantity <= available, "Sale exceeds eligible shares")
        require(trading_calendar.holding_days(lot["acquired_at"], context["decision_at"], terms["holding"])
                >= terms["holding"]["minimum_days"], "Lot remains locked")
        sold.setdefault(code, []).append(trade)
    for code, trades in sold.items():
        terms = context["fee_contracts"][code]
        validate_redemption_fee_scope(terms,len(trades))
        product_lots = [lot for lot in lots.values() if lot["code"] == code and fees.number(lot["shares"])-fees.number(lot["reserved_shares"]) > 0]
        total = sum((fees.number(lot["shares"])-fees.number(lot["reserved_shares"]) for lot in product_lots), ZERO)
        quantity = sum((fees.number(trade["shares"]) for trade in trades), ZERO)
        held_total = sum((fees.number(lot["shares"]) for lot in lots.values() if lot["code"] == code), ZERO)
        full = quantity == held_total
        require(_action_supported(terms, "sell_full" if full else "sell_partial"), "Source evidence for redemption action required")
        minimum, residual = terms.get("minimum_redemption_shares"), terms.get("minimum_remaining_shares")
        if minimum is None or residual is None:
            require(full and terms.get("full_redemption_allowed") is True, "Source redemption minima required for partial sale")
        else:
            minimum, residual = fees.number(minimum), fees.number(residual)
            require(quantity >= minimum or full and terms.get("full_redemption_allowed") is True, "Redemption is below source minimum")
            require(held_total-quantity == 0 or held_total-quantity >= residual, "Redemption leaves forbidden source residual")
        method = terms.get("redemption_allocation", {}).get("method")
        unambiguous = len(product_lots) == 1 or quantity == total
        require(unambiguous or method in {"specific_lot", "fifo", "lifo"}, "Source redemption allocation required")
        if method in {"fifo", "lifo"} and not unambiguous:
            expected = _split_redemption(product_lots, quantity, method)
            require({row["lot_id"]: fees.number(row["shares"]) for row in trades}
                    == {row["lot_id"]: fees.number(row["shares"]) for row in expected}, "Redemption violates source lot order")
    available = fees.number(snapshot["available_cash"])+fees.number(funding)
    require(fees.number(funding) >= 0 and available >= 0, "Nonnegative source funding required")
    seen = set()
    for trade in action["buys"]:
        require(type(trade) is dict and set(trade) == {"code", "cash_debit"}, "Buy requires code and cash debit")
        code = trade["code"]
        require(code in assets and code not in seen and code not in sold and code in context["purchase_eligible_codes"],
                "Ineligible, repeated or same-stage reversal buy")
        seen.add(code)
        terms, asset = context["fee_contracts"][code], assets[code]
        require(_action_supported(terms, "buy"), "Source evidence for purchase action required")
        require(asset.get("min_buy") is not None, "Source subscription minimum required")
        debit, step = fees.number(trade["cash_debit"]), fees.number(terms["trade_precision"]["money_step"])
        require(debit > 0 and debit == fees.floor(debit, step), "Buy differs from source precision")
        require(asset["buyable"] and asset["buy_allowed"] and terms["buyable"] and debit >= fees.number(asset["min_buy"]), "Buy violates eligibility or minimum")
        if asset.get("max_buy") is not None:
            require(debit <= fees.number(asset["max_buy"]), "Buy exceeds source cap")
        available -= debit
        require(available >= 0, "Buy spends unreceived, reserved or unconfirmed cash")
    return True


def _amount_menu(low, high, step, boundaries):
    require(step > 0, "Positive source precision required")
    low = max(step, (low/step).to_integral_value(rounding=ROUND_CEILING)*step)
    high = fees.floor(high, step)
    if high < low:
        return [], True
    count = int((high-low)/step)+1
    if count <= 16:
        return [low+index*step for index in range(count)], True
    values = {low, high}
    values.update(fees.floor(high*Decimal(index)/4, step) for index in (1, 2, 3))
    for bound in boundaries:
        rounded = fees.floor(fees.number(bound), step)
        values.update((rounded-step, rounded, rounded+step))
    return sorted(value for value in values if low <= value <= high), False


def _fee_boundaries(rule):
    if rule["kind"] != "amount_tiers":
        return []
    return [band[key] for band in rule["bands"] for key in ("minimum", "maximum") if band.get(key) is not None]


def build_source_actions(context):
    """Exhaust the disclosed finite family before reading forecast errors.

    Large precision domains use a declared finite source menu, with no claim
    about all possible orders. Budget failure publishes no incumbent family.
    """
    snapshot = context["snapshot"]
    budget = context["spec"]["decision"]["max_current_actions"]
    require(type(budget) is int and budget >= 1, "Declared action budget required")
    levels = sorted(set(fees.number(value) for value in context["model_request"]["allocation_policy"]["funding_levels"]))
    require(levels and levels[0] == 0 and all(value >= 0 for value in levels), "Explicit zero funding group required")
    assets = {row["code"]: row for row in context["model_request"]["assets"]}
    out = {"status": "frozen", "scope": "declared_finite_source_cash_share_actions_for_complete_path_MPC", "menu_rule": MENU_RULE,
           "context_hash": context["context_hash"], "funding_options": [], "required_actions": [],
           "global_investment_optimality_claimed": False, "current_orders_use_additional_funding": False}
    for funding in levels:
        menus, audits, counts, descriptors, blocked = [], [], [], [], []
        current_cash = fees.number(snapshot["available_cash"])+funding
        for code, asset in sorted(assets.items()):
            terms = context["fee_contracts"][code]
            choices, precision_complete = [{"buys": [], "sells": []}], True
            lot_descriptor, unenumerated_count = None, 0
            if code in context["purchase_eligible_codes"] and asset["buyable"] and asset["buy_allowed"] and terms["buyable"]:
                upper = min(current_cash, fees.number(asset["max_buy"])) if asset.get("max_buy") is not None else current_cash
                if not _action_supported(terms, "buy") or asset.get("min_buy") is None:
                    out["required_actions"].append({"action": "complete_source_purchase_action", "code": code,
                        "missing_fields": terms.get("action_evidence", {}).get("buy", {}).get("missing_fields", ["min_buy"])})
                    upper = ZERO
                values, complete = _amount_menu(fees.number(asset["min_buy"]) if asset.get("min_buy") is not None else fees.number(terms["trade_precision"]["money_step"]), upper, fees.number(terms["trade_precision"]["money_step"]), _fee_boundaries(terms["subscription"]))
                precision_complete &= complete
                choices += [{"buys": [{"code": code, "cash_debit": ledger.decimal(value)}], "sells": []} for value in values]
            product_lots = [lot for lot in snapshot["positions"] if lot["code"] == code and fees.number(lot["shares"])-fees.number(lot["reserved_shares"]) > 0]
            total = sum((fees.number(lot["shares"])-fees.number(lot["reserved_shares"]) for lot in product_lots), ZERO)
            if total > 0 and asset["sellable"] and terms["sellable"]:
                if len(product_lots) > 1:
                    try:
                        validate_redemption_fee_scope(terms,len(product_lots))
                    except ContractError:
                        out["required_actions"].append({"action":"capture_source_redemption_fee_and_rounding_application","code":code})
                        precision_complete = False
                method = terms.get("redemption_allocation", {}).get("method")
                step, price = fees.number(terms["trade_precision"]["share_step"]), fees.number(snapshot["prices"][code])
                boundaries = [fees.number(value)/price for value in _fee_boundaries(terms["redemption"])]
                minima_known = terms.get("minimum_redemption_shares") is not None and terms.get("minimum_remaining_shares") is not None
                if minima_known:
                    boundaries += [fees.number(terms["minimum_redemption_shares"]), total-fees.number(terms["minimum_remaining_shares"])]
                if not minima_known or not _action_supported(terms, "sell_partial"):
                    out["required_actions"].append({"action": "complete_source_partial_redemption_action", "code": code,
                        "missing_fields": terms.get("action_evidence", {}).get("sell_partial", {}).get("missing_fields", ["minimum_redemption_shares", "minimum_remaining_shares"])})
                    choices.append({"buys": [], "sells": _split_redemption(product_lots, total, "fifo")})
                    precision_complete = False
                elif len(product_lots) == 1 or method in {"fifo", "lifo"}:
                    values, complete = _amount_menu(step, total, step, boundaries)
                    precision_complete &= complete
                    choices += [{"buys": [], "sells": _split_redemption(product_lots, value, method)} for value in values]
                elif method == "specific_lot":
                    lot_menus = []
                    for lot in sorted(product_lots, key=lambda row: row["lot_id"]):
                        values, complete = _amount_menu(step, fees.number(lot["shares"])-fees.number(lot["reserved_shares"]), step, boundaries)
                        precision_complete &= complete
                        lot_menus.append([None]+[{"lot_id": lot["lot_id"], "shares": ledger.decimal(value)} for value in values])
                    size = math.prod(len(menu) for menu in lot_menus)
                    lot_descriptor = lot_menus
                    if size > budget:
                        unenumerated_count = size-1
                        blocked.append({"code":code,"required":size,"budget":budget})
                    else:
                        choices += [{"buys": [], "sells": [row for row in selected if row]} for selected in itertools.product(*lot_menus) if any(selected)]
                else:
                    # Full redemption is invariant to unknown allocation;
                    # partial multi-lot choices need source completion.
                    out["required_actions"].append({"action": "capture_source_redemption_lot_allocation", "code": code})
                    choices.append({"buys": [], "sells": _split_redemption(product_lots, total, "fifo")})
                    precision_complete = False
            valid, rejected = [], []
            for choice in choices:
                try:
                    validate_source_action(context, choice, funding=funding)
                except ContractError as error:
                    rejected.append({"action": choice, "reason": str(error)})
                else:
                    valid.append(choice)
            menus.append(valid)
            counts.append(len(valid)+unenumerated_count)
            descriptors.append({"code":code,"source_contract_hash":fingerprint(terms),"choices":valid,
                                "specific_lot_menus":lot_descriptor,"choice_count_upper":counts[-1]})
            audits.append({"code": code, "choices": len(valid), "source_precision_domain_complete": precision_complete, "rejected_source_choices": rejected})
        size = math.prod(counts)
        descriptor = {"proposed_contribution":ledger.decimal(funding),"menus":descriptors,
            "constraint_hash":fingerprint(context["spec"]["constraints"]),"combination_count_upper":size,"menu_rule":MENU_RULE}
        if size > budget or blocked:
            gap = {"action":"increase_declared_current_action_budget_or_explicitly_narrow_scope","required":size,
                   "budget":budget,"funding":ledger.decimal(funding),"lot_menu_gaps":blocked}
            out["required_actions"].append(gap)
            out["funding_options"].append({"proposed_contribution":float(funding),"actions":[],
                "build_status":"budget_partial","current_action_scope_complete":False,"source_precision_domain_complete":False,
                "combination_count":size,"action_count_upper":size,"descriptor":descriptor,"descriptor_hash":fingerprint(descriptor),
                "menus":audits,"rejected_combinations":[],"required_actions":[gap],
                "capital_before_trade":ledger.decimal(fees.number(snapshot["equity"])+funding),"available_cash":ledger.decimal(current_cash)})
            continue
        actions, rejected = {}, []
        for selected in itertools.product(*menus):
            action = {"buys": [row for choice in selected for row in choice["buys"]], "sells": [row for choice in selected for row in choice["sells"]]}
            try:
                validate_source_action(context, action, funding=funding)
            except ContractError as error:
                rejected.append({"action": action, "reason": str(error)})
            else:
                actions[fingerprint(action)] = action
        out["funding_options"].append({"proposed_contribution": float(funding), "actions": [actions[key] for key in sorted(actions)],
            "build_status":"complete","action_count_upper":size,"descriptor":descriptor,"descriptor_hash":fingerprint(descriptor),"required_actions":[],
            "current_action_scope_complete": True, "source_precision_domain_complete": all(row["source_precision_domain_complete"] for row in audits),
            "combination_count": size, "menus": audits, "rejected_combinations": rejected,
            "capital_before_trade": ledger.decimal(fees.number(snapshot["equity"])+funding), "available_cash": ledger.decimal(current_cash)})
    out["required_actions"] = list({fingerprint(row): row for row in out["required_actions"]}.values())
    registry = [{"proposed_contribution":row["proposed_contribution"],"descriptor_hash":row["descriptor_hash"],
                 "action_count_upper":row["action_count_upper"]} for row in out["funding_options"]]
    out["funding_registry"] = registry
    out["funding_registry_hash"] = fingerprint(registry)
    if next(row for row in out["funding_options"] if row["proposed_contribution"] == 0)["build_status"] != "complete":
        out.update(status="partial",reason="zero_funding_action_scope_incomplete")
    out["action_family_hash"] = fingerprint(out["funding_options"])
    return out
