"""Original HTML/PDF dealing clauses and channel tables, independent of product."""
import re
from decimal import Decimal
from urllib.parse import urlsplit
from contracts import fields, require, instant
from source_documents import read_extracted_document
import trading_calendar

ADAPTER = "document_formats_v2"


def canonical_investor_scope(value):
    if value in ("retail", "普通投资者", "个人投资者", "普通个人投资者", "全部投资者", "不限"):
        return "retail"
    if value in ("personal_pension", "个人养老金投资者", "个人养老金账户"):
        return "personal_pension"
    return "unknown"


def parse_interval(condition, *, holding):
    """Preserve monetary and holding-day endpoint inclusions exactly."""
    text = re.sub(r"\s+", "", condition).replace("≤", "<=").replace("≥", ">=").replace("＜", "<").replace("＞", ">")
    quantity = r"(\d+(?:\.\d+)?)(万|亿)?(?:元|天|日)?"
    comparisons = []
    for match in re.finditer(r"(大于等于|小于等于|大于|小于)" + quantity, text):
        comparisons.append(({"大于等于": ">=", "小于等于": "<=", "大于": ">", "小于": "<"}[match[1]], match[2], match[3]))
    if not comparisons:
        for match in re.finditer(r"[NMnm](<=|>=|<|>)" + quantity, text):
            comparisons.append((match[1], match[2], match[3]))
        for match in re.finditer(quantity + r"(<=|>=|<|>)[NMnm]", text):
            comparisons.append(({"<=": ">=", ">=": "<=", "<": ">", ">": "<"}[match[3]], match[1], match[2]))
    require(comparisons and not any(unit in text for unit in ("年", "月")), "Unsupported fee applicability interval: " + condition)
    lower, upper, low_included, high_included = Decimal(0), None, True, False
    lower_count = upper_count = 0
    for operator, number, scale in comparisons:
        value = Decimal(number) * {None: 1, "万": 10000, "亿": 100000000}[scale]
        require(not holding or value == value.to_integral_value(), "Fractional holding-day boundary")
        if operator.startswith(">"):
            lower, low_included, lower_count = value, operator == ">=", lower_count + 1
        else:
            upper, high_included, upper_count = value, operator == "<=", upper_count + 1
    require(lower_count <= 1 and upper_count <= 1 and (upper is None or upper > lower), "Conflicting fee bounds")
    return {"minimum": str(lower), "minimum_inclusive": low_included,
            "maximum": None if upper is None else str(upper), "maximum_inclusive": high_included}


def _text(document):
    return "\n".join(block["text"] for block in document["blocks"] if block["kind"] not in ("pdf_line", "table_cell"))


def _pairs(document):
    output = {}
    for block in document["blocks"]:
        if block["kind"] == "table_row":
            cells = block.get("cells", [])
            for offset in range(0, len(cells)-1, 2):
                output.setdefault(cells[offset].strip(), []).append((cells[offset+1].strip(), block["locator"]))
    return output


def _unique(pairs, key):
    values = pairs.get(key, [])
    require(values and len({value for value, _ in values}) == 1, "Missing or conflicting platform field: "+key)
    return values[0]


def _money(text, unit):
    if text in ("无限额", "无上限"):
        return None
    match = re.fullmatch(r"\s*([0-9,]+(?:\.\d+)?)\s*(万|亿)?\s*"+unit+r"\s*", text)
    require(match is not None, "Undisclosed or unsupported quantity: "+text)
    return str(Decimal(match[1].replace(",", "")) * {None: 1, "万": 10000, "亿": 100000000}[match[2]])


def _rate(text):
    percentage = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[%％](?:\s*\([^)]*\))?\s*", text)
    if percentage:
        return {"kind": "percentage", "rate": str(Decimal(percentage[1])/100)}
    fixed = re.fullmatch(r"\s*(?:每笔)?([0-9,]+(?:\.\d+)?)\s*元(?:/笔)?\s*", text)
    require(fixed is not None, "Unsupported published fee charge")
    return {"kind": "fixed", "amount": fixed[1].replace(",", "")}


def _fee_table(document, redemption):
    expected = "适用期限" if redemption else "适用金额"
    headers = [b for b in document["blocks"] if b["kind"] == "table_row" and b.get("cells") and b["cells"][0] == expected]
    blocks_by_locator = {block["locator"]: block for block in document["blocks"]}
    subject = "赎回" if redemption else "申购"
    scoped = [row for row in headers if any(subject in blocks_by_locator[locator]["text"]
              for locator in row.get("heading_locators", []) if locator in blocks_by_locator)]
    if scoped:
        headers = scoped
    require(len(headers) == 1, "Unambiguous platform fee table required")
    header = headers[0]
    prefix = header["locator"].split("/row/")[0]+"/row/"
    rows = [b for b in document["blocks"] if b["kind"] == "table_row" and b["locator"].startswith(prefix) and b is not header]
    heading_cells = [part for value in header["cells"] for part in value.split("|")]
    columns = [i for i, value in enumerate(heading_cells) if "优惠费率" in value]
    if not columns:
        columns = [i for i, value in enumerate(heading_cells) if value in ("费率", "赎回费率", "申购费率")]
    require(len(columns) == 1 and rows, "Published channel fee column is ambiguous")
    bands = []
    for block in rows:
        cells = [part.strip() for cell in block["cells"] for part in cell.split("|")]
        shared_fixed = len(cells) == 2 and re.fullmatch(r"每笔[0-9,]+(?:\.\d+)?元", cells[1]) is not None
        require(len(cells) > columns[0] or shared_fixed, "Incomplete platform fee row")
        condition, fee = cells[0].replace(" ", ""), _rate(cells[1] if shared_fixed else cells[columns[0]])
        if condition in ("---", "--", "不限") and len(rows) == 1 and fee["kind"] == "percentage" and Decimal(fee["rate"]) == 0:
            return {"kind": "percentage", "rate": "0"}, [block["locator"]]
        bands.append({**parse_interval(condition, holding=redemption), "fee": fee})
    return {"kind": "holding_tiers" if redemption else "amount_tiers", "bands": bands}, [row["locator"] for row in rows]


def inspect_document_formats(code, sources, artifacts, *, as_of):
    """Re-extract product facts from original layouts; retain concrete gaps."""
    fields(sources, {"product", "prospectus", "platform", "cutoff", "calendars"}, {"calendar_roles"}, label="issuer/platform contract sources")
    docs = {name: read_extracted_document(sources[name], artifacts) for name in ("product", "prospectus", "platform", "cutoff")}
    require(all(instant(doc["retrieved_at"]) <= instant(as_of) for doc in docs.values()), "Future-captured dealing source")
    product = re.sub(r"\s+", "", _text(docs["product"]))
    prospectus = re.sub(r"\s+", "", _text(docs["prospectus"]))
    cutoff = re.sub(r"\s+", "", _text(docs["cutoff"])).replace("：", ":")
    require(all(docs[name]["source_id"].startswith("issuer_") for name in ("product", "prospectus")), "Original issuer documents required")
    require(docs["platform"]["source_id"] == "eastmoney_fees" and re.fullmatch(r"/jjfl_"+re.escape(code)+r"\.html", urlsplit(docs["platform"]["url"]).path), "Platform fee page belongs to another product")
    children = set(re.findall(r"下属基金代码(\d{6})", product))
    require(code in children or not children and (re.search(r"基金代码[：:]?"+re.escape(code)+r"(?!\d)", product)
            or any(code in block.get("cells", []) for block in docs["product"]["blocks"])), "Issuer product code role differs")
    product_pairs = _pairs(docs["product"])
    name_values = [value for key in ("基金简称", "基金名称", "基金全称") for value, _ in product_pairs.get(key, [])]
    name_values += [block["text"] for block in docs["product"]["blocks"] if block["kind"] in ("h1", "h2", "h3", "h4", "h5", "h6")]
    name_values += [" ".join(block["cells"]) for block in docs["product"]["blocks"] if block.get("cells") and code in block["cells"]]
    classes = {share for value in name_values for share in re.findall(r"(?<![A-Za-z])([A-Z])(?:类|$|[（(])", value.strip())}
    if not classes:
        classes = set(re.findall(r"份额类别[：:]?([A-Z])", product))
    if not classes:
        child = re.findall(r"下属基金简称(.{1,100}?)下属基金代码" + re.escape(code), product)
        classes = {share for value in child for share in re.findall(r"([A-Z])(?:类)?$", value)}
    if not classes:
        classes = set(re.findall(r"[（(](?:[^）)]*?基金[^）)]*?)([A-Z])[）)]基金产品资料|[（(]([A-Z])类份额[）)]基金产品资料", product))
        classes = {share for values in classes for share in values if share}
    require(len(classes) == 1, "Issuer product share class is ambiguous")
    share_class = next(iter(classes))
    platform_title = re.sub(r"\s+", "", _text(docs["platform"]))[:240]
    require(re.search(re.escape(share_class) + r"[（(]" + re.escape(code) + r"[）)]基金费率", platform_title),
            "Channel document primary title differs from issuer code/share class")
    legal_names = [value for key in ("基金全称", "基金名称") for value, _ in product_pairs.get(key, [])]
    if not legal_names:
        legal_names = re.findall(r"^(?:\d+/\d+)?(.{1,160}?基金)(?:[（(]|基金产品资料)", product)
    primary_title = re.sub(r"^(?:\d+/\d+)?", "", prospectus[:240])
    require(any(primary_title.startswith(re.sub(r"\s+", "", value).removesuffix(share_class)) for value in legal_names), "Issuer prospectus primary title lacks the product subject")
    require(re.search(r"(?:交易币种|基金币种|计价货币|申购币种)[：:]?人民币", product), "Issuer CNY dealing currency is not established")
    required, statuses = [], {}
    def fact(field, operation, source="prospectus"):
        try:
            value = operation()
        except ValueError as error:
            statuses[field] = {"status": "unknown_or_conflicting", "reason": str(error), "document_ref": sources[source]}
            required.append({"action": "inspect_original_dealing_fact", "code": code, "field": field, "reason": str(error), "document_ref": sources[source]})
            return None
        statuses[field] = {"status": "source_extracted", "document_ref": sources[source]}
        return value
    def one(pattern, label, text=prospectus):
        values = set(re.findall(pattern, text))
        require(len(values) == 1, "Missing or conflicting source clause: " + label)
        return next(iter(values))
    def clock(name):
        require(re.search(r"T\+n日.{0,70}第n个工作日.{0,30}(?:不包含T日|不含T日)", prospectus, re.I), "T+n day convention is not established")
        pattern = (r"T\+(\d+)日内(?:为投资[者人])?.{0,60}?(?:有效性进行确认|进行有效确认|确认)" if name == "confirmation" else r"T\+(\d+)日(?:[（(]包括该日[）)])?内(?:支付|划出|划付)赎回款")
        return {"lag_days": int(one(pattern, name)), "day_basis": "trading_days", "normal_conditions_only": True}
    def calendars_for_roles():
        def document_refs(value):
            if type(value) is dict:
                if "document_ref" in value:
                    yield value["document_ref"]
                else:
                    for child in value.values():
                        yield from document_refs(child)
            elif type(value) is list:
                for child in value:
                    yield from document_refs(child)
        require(all(instant(read_extracted_document(ref, artifacts)["retrieved_at"]) <= instant(as_of)
                    for ref in document_refs(sources["calendars"])), "Future-captured exchange calendar source")
        calendars = {item["id"]: trading_calendar.from_source(item["binding"], artifacts) for item in sources["calendars"]}
        require(len(calendars) == len(sources["calendars"]), "Duplicate calendar source identity")
        for item in sources["calendars"]:
            if item["binding"].get("kind") == "exchange_annual_notice":
                require(item["id"] in ("SSE", "SZSE"), "This annual holiday grammar only establishes domestic exchange calendars")
                require(all(read_extracted_document(ref, artifacts)["source_id"] == item["id"].lower()+"_trading_calendar"
                            for ref in document_refs(item["binding"])), "Exchange calendar source is labelled as another market")
            else:
                require(calendars[item["id"]]["id"] == item["id"], "Calendar role label differs from original source identity")
        roles = sources.get("calendar_roles")
        if roles is None:
            require(re.search(r"上海证券交易所和深圳证券交易所同时开放交易.{0,30}(?:开放日|工作日)|工作日.{0,50}上海证券交易所[和、]深圳证券交易所.{0,20}交易日", prospectus), "Product calendar composition needs issuer evidence")
            foreign_closure = r"(?:非港股通交易日|境外.{0,40}(?:休市|节假日)|香港.{0,40}(?:休市|节假日))"
            dealing_suspension = r"(?:不开放|暂停|不办理|不接受).{0,30}(?:申购|赎回)"
            require(not re.search(foreign_closure + r".{0,120}" + dealing_suspension + r"|" + dealing_suspension + r".{0,120}" + foreign_closure, prospectus),
                    "Issuer foreign-market dealing exceptions need explicit complete calendar roles")
            roles = {role: {"ids": ["SSE", "SZSE"]} for role in ("pricing", "confirmation", "settlement")}
        fields(roles, {"pricing", "confirmation", "settlement"}, label="source calendar roles")
        words = {"SSE": "上海证券交易所", "SZSE": "深圳证券交易所", "HKEX": "香港", "NYSE": "纽约", "NASDAQ": "纳斯达克", "US": "美国"}
        result = {}
        for role, binding in roles.items():
            fields(binding, {"ids"}, {"locator"}, label="issuer calendar role binding")
            identities = binding["ids"]
            require(type(identities) is list and identities and len(set(identities)) == len(identities), "Explicit calendar members required")
            clause = prospectus
            if "calendar_roles" in sources:
                require("locator" in binding, "Calendar composition requires an original issuer clause locator")
                blocks = [block for block in docs["prospectus"]["blocks"] if block["locator"] == binding["locator"]]
                require(len(blocks) == 1, "Issuer calendar clause is missing or ambiguous")
                clause = re.sub(r"\s+", "", blocks[0]["text"])
                require("开放日" in clause if role == "pricing" else "工作日" in clause, "Issuer clause does not define this calendar role")
                mentioned = {identity for identity, word in words.items() if word in clause}
                if mentioned & {"NYSE", "NASDAQ"}:
                    mentioned.discard("US")
                require(set(identities) == mentioned, "Calendar role omits or invents a market named by its issuer clause")
            require(all(identity in calendars and identity in words and words[identity] in clause for identity in identities), "Calendar role lacks issuer market evidence")
            result[role] = trading_calendar.intersection([calendars[identity] for identity in identities], "issuer-"+role+"-calendar")
            require(result[role]["timezone"] == "Asia/Shanghai", "Foreign exchange dates require an issuer projection to the TT dealing date")
        return result
    roles = fact("calendar_roles", calendars_for_roles)
    calendar = None if roles is None else roles["pricing"]
    confirmation, settlement = (fact(name, lambda name=name: clock(name)) for name in ("confirmation", "settlement"))
    if roles:
        for name, rule in (("confirmation", confirmation), ("settlement", settlement)):
            if rule is not None:
                rule["calendar"] = roles[name]
    def order_cutoff():
        require("下一个工作日" in cutoff and ("非交易日" in cutoff or "周末" in cutoff), "Source-supported deferred order rule required")
        return one(r"(?:工作日|交易日)([0-2]\d:[0-5]\d)前的交易", "channel cutoff", cutoff)
    cutoff_time = fact("order_cutoff_local", order_cutoff, "cutoff")
    def holding():
        require(re.search(r"持有期自(?:该)?基金份额申购确认日至赎回确认日[（(]不含该日[）)]", prospectus), "Holding-period endpoints are not established")
        locks = set(re.findall(r"最[短低]持有期(?:为|是)?(\d+)(?:天|日)", prospectus))
        require(len(locks) <= 1 and (locks or "普通开放式" in product or "不设最低持有" in prospectus), "Holding lock is not established")
        return {"minimum_days": int(next(iter(locks))) if locks else 0, "day_basis": "calendar_days", "start_inclusive": True, "end_inclusive": False, "end_event": "confirmation_date"}
    holding_rule = fact("holding", holding)
    def precision():
        half_shares = bool(re.search(r"申购(?:份数|份额).{0,70}四舍五入.{0,30}小数点后[二两2]位", prospectus) and re.search(r"由此产生的(?:收益或损失|误差).{0,25}基金(?:财产|资产)承担", prospectus))
        down_shares = bool(re.search(r"申购份额的计算结果保留到小数点后2位,小数点后第3位开始舍去,舍去部分归基金财产", prospectus))
        require(half_shares != down_shares, "Share rounding rule is missing or conflicting")
        half_money = bool(re.search(r"赎回(?:费用|费|总额).{0,60}四舍五入.{0,30}小数点后[二两2]位", prospectus))
        down_money = bool(re.search(r"赎回金额的计算方式.{0,250}计算结果保留到小数点后2位,小数点后第3位开始舍去,舍去部分归基金财产", prospectus))
        require(half_money != down_money, "Redemption net precision is missing or conflicting")
        if down_money:
            require("赎回费用=赎回总额" in prospectus and "申购费用=申购金额-净申购金额" in prospectus, "Unrounded fee formula is not established")
        return {"money_step": "0.01", "share_step": "0.01", "fee_rounding": "half_up" if half_money else "unrounded",
                "share_rounding": "half_up_fund" if half_shares else "down_fund",
                "redemption_net_rounding": "half_up" if half_money else "down_fund"}
    precision_rule = fact("trade_precision", precision)
    investor_type = "unknown"
    class_restriction = re.search(re.escape(share_class) + r"类(?:基金)?份额.{0,60}(?:仅供|仅面向|仅向|专供|限于).{0,30}机构投资[者人]", prospectus)
    pension_restriction = re.search(re.escape(share_class) + r"类(?:基金)?份额.{0,60}(?:仅供|仅面向|专供|限于).{0,30}个人养老金", prospectus)
    if pension_restriction or share_class == "Y" and re.search(r"个人养老金资金账户|Y类.{0,80}个人养老金", prospectus):
        investor_type = "personal_pension"
    elif not class_restriction and share_class != "Y" and re.search(r"(?:投资人|投资者|募集对象|发售对象).{0,120}(?:个人投资者|个人投资人)", prospectus) and re.search(share_class+r"类(?:基金)?份额", prospectus):
        investor_type = "retail"
    if investor_type == "unknown":
        fact("subject.investor_type", lambda: require(False, "Issuer individual-investor applicability is not established"))
    pairs = _pairs(docs["platform"])
    bought, _ = _unique(pairs, "申购状态")
    sold, _ = _unique(pairs, "赎回状态")
    require(bought in ("开放申购", "暂停申购", "限大额", "限制大额申购") and sold in ("开放赎回", "暂停赎回"), "Unsupported published dealing status")
    minimum, _ = _unique(pairs, "申购起点")
    maximum, _ = _unique(pairs, "日累计申购限额")
    subscription, sub_locations = _fee_table(docs["platform"], False)
    redemption, red_locations = _fee_table(docs["platform"], True)
    from fee_contract import validate_rule
    validate_rule(subscription); validate_rule(redemption)
    if share_class == "C":
        require("C类基金份额不收取申购费" in prospectus and subscription["kind"] == "percentage" and Decimal(subscription["rate"]) == 0, "Issuer and platform C-class subscription costs disagree")
    normalized = {"subject": {"code": code, "share_class": share_class, "currency": "CNY", "channel": "TT", "investor_type": investor_type},
        "subscription": subscription, "redemption": redemption, "min_buy": _money(minimum, "元"), "max_buy": _money(maximum, "元"),
        "max_buy_scope": "daily_cumulative_published_limit", "buyable": bought in ("开放申购", "限大额", "限制大额申购"), "sellable": sold == "开放赎回",
        "holding": holding_rule, "settlement": settlement, "confirmation": confirmation,
        "execution_calendar": calendar, "order_cutoff_local": cutoff_time,
        "trade_precision": precision_rule}
    def acquisition():
        require(holding_rule is not None and re.search(r"确认登记|登记机构.{0,40}确认|确认.{0,40}登记机构", prospectus), "Source registration acquisition rule is not established")
        return {"holding_start": "confirmation_date", "ownership_start": "confirmation_date"}
    normalized["acquisition_rule"] = fact("acquisition_rule", acquisition)
    def allocation():
        methods = [method for method, supported in (("fifo", bool(re.search(r"先进先出|先申购.{0,30}先赎回", prospectus))), ("lifo", "后进先出" in prospectus), ("specific_lot", bool(re.search(r"投资[者人]可指定.{0,15}赎回批次", prospectus)))) if supported]
        require(len(methods) == 1, "Redemption lot allocation is not established")
        return {"method": methods[0]}
    # Unknown allocation does not invent FIFO. Whole-fund liquidation may
    # still be unambiguous; partial multi-lot actions require this evidence.
    try:
        normalized["redemption_allocation"] = allocation()
    except ValueError:
        normalized["redemption_allocation"] = {"method": "unknown"}
    for field, label in (("minimum_redemption_shares", "最小赎回份额"), ("minimum_remaining_shares", "部分赎回最低保留份额")):
        text, locator = _unique(pairs, label)
        try:
            normalized[field] = _money(text, "份")
            require(normalized[field] is not None, "A share threshold cannot be unlimited")
            statuses[field] = {"status": "source_extracted", "locator": locator}
        except ValueError:
            normalized[field] = None
            statuses[field] = {"status": "unknown_in_published_source", "literal": text, "locator": locator}
            required.append({"action": "obtain_current_platform_dealing_threshold", "code": code, "field": field, "source_literal": text, "document_ref": sources["platform"], "locator": locator})
    if normalized["max_buy"] is not None:
        required.append({"action": "bind_remaining_daily_subscription_limit_to_account_usage", "code": code, "limit": normalized["max_buy"]})
    normalized["full_redemption_allowed"] = bool(re.search(r"投资[者人]可将其全部或部分(?:基金)?份额赎回|可(?:以)?赎回全部(?:或部分)?基金份额", prospectus))
    partial_fields = {"minimum_redemption_shares", "minimum_remaining_shares"}
    common = sorted({item["field"] for item in required if "field" in item and item["field"] not in partial_fields})
    buy_missing = common + (["remaining_daily_subscription_limit"] if normalized["max_buy"] is not None else [])
    full_missing = common + ([] if normalized["full_redemption_allowed"] else ["full_redemption_permission"])
    partial_missing = common + sorted({item["field"] for item in required if item.get("field") in partial_fields})
    normalized["action_evidence"] = {name: {"status": "needs_research" if missing else "source_supported", "missing_fields": sorted(set(missing))}
        for name, missing in (("buy", buy_missing), ("sell_full", full_missing), ("sell_partial", partial_missing))}
    return {"normalized": normalized, "quote_observed_at": docs["platform"]["retrieved_at"], "rule_effective_dates": {"status": "unknown"},
            "required_actions": required, "field_statuses": statuses,
            "evidence_scope": "reextracted_current_public_terms_not_account_specific_executable_quote",
            "fee_locations": {"subscription": sub_locations, "redemption": red_locations},
            "normal_processing_exceptions": "issuer_discloses_possible_redemption_deferral_no_unconditional_payment_deadline"}
