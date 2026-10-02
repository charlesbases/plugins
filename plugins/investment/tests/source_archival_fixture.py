"""Synthetic virtual-time archives produced by the actual source adapters.

Only source transport and the clock are controlled. No model rows, timestamps
of first observation, extracted documents or readiness results are injected.
"""
import contextlib
import datetime as dt
import hashlib
import io
import json
import re
import sys
import urllib.parse
from unittest.mock import patch

import contracts
import industry_data
import source_documents
import source_fetch
from contracts import fingerprint, instant
from state_store import Store

CN = dt.timezone(dt.timedelta(hours=8))
IDENTITY_URL = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000300factsheet.pdf"
INDEX_NAME = "SyntheticGold"
SECTOR_RELATION_QUOTE = "本指数反映黄金行业的市场表现。"
ECONOMIC_QUOTE = "黄金行业政策现予印发。"
TAXONOMY_QUOTE = "SyntheticTaxonomy v1：黄金行业分类标签黄金。"


def unicodePDF(lines):
    """Encode actual Chinese source text in a native machine-readable PDF."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=max(120, len(lines)*20+40))
    cid = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/CIDFontType0"),
        NameObject("/BaseFont"): NameObject("/STSong-Light"), NameObject("/CIDSystemInfo"): DictionaryObject({
            NameObject("/Registry"): TextStringObject("Adobe"), NameObject("/Ordering"): TextStringObject("GB1"), NameObject("/Supplement"): NumberObject(4)})})
    characters = sorted(set("".join(lines)))
    pairs = "\n".join("<"+char.encode("utf-16-be").hex()+"> <"+char.encode("utf-16-be").hex()+">" for char in characters)
    cmap = DecodedStreamObject()
    cmap.set_data(("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n/CMapName /Unicode def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"+str(len(characters))+" beginbfchar\n"+pairs+
        "\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend").encode("ascii"))
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/STSong-Light"), NameObject("/Encoding"): NameObject("/UniGB-UCS2-H"),
        NameObject("/DescendantFonts"): ArrayObject([writer._add_object(cid)]), NameObject("/ToUnicode"): writer._add_object(cmap)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    operators = ["BT /F1 12 Tf 10 "+str(len(lines)*20+10-index*20)+" Td <"+line.encode("utf-16-be").hex()+"> Tj ET"
                 for index, line in enumerate(lines)]
    stream.set_data("\n".join(operators).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


def _identity_pdf():
    return unicodePDF(["SyntheticGold 000300 CNY", "指数简介", SECTOR_RELATION_QUOTE, TAXONOMY_QUOTE])


def _span():
    end = dt.datetime.now(CN).date()-dt.timedelta(days=1)
    return end-dt.timedelta(days=319), end


def holdings_url(code):
    return "https://www.cmfchina.com/fundarticle/holdings_"+code+".html"


def _capture(url, source_id, body, media, at):
    text = body.decode("utf8") if media != "application/pdf" else None
    return {"registry_source_id": source_id, "registry_hash": fingerprint(source_fetch.load_registry()),
            "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
            "content_type": media, "encoding": "utf-8" if text is not None else None,
            "transport": "HTTPS_default_certificate_validation", "retrieved_at": at,
            "bytes": len(body), "raw_sha256": hashlib.sha256(body).hexdigest(),
            "text_sha256": hashlib.sha256(text.encode("utf8")).hexdigest() if text is not None else None,
            "raw_bytes": body, "text": text}


def extra_capture(url, source_id, observed_at=None, **kwargs):
    """Additional controlled public sources; None delegates to the base fixture."""
    at = observed_at or contracts.utc_now()
    if source_id == "eastmoney_nav":
        code = re.search(r"/([0-9]{6})\.js$", urllib.parse.urlsplit(url).path).group(1)
        first,end = _span()
        last = min(instant(at).astimezone(CN).date(),end)
        points,cumulative = [],[]
        for offset in range((last-first).days+1):
            day = first+dt.timedelta(days=offset)
            stamp = int(dt.datetime.combine(day,dt.time(),CN).timestamp()*1000)
            points.append({"x":stamp,"y":2.,"unitMoney":"","equityReturn":0.})
            cumulative.append([stamp,2.])
        text = 'var fS_code = '+json.dumps(code)+';'+chr(10)+'var fS_name = "Synthetic constant NAV fixture";'+chr(10)
        text += 'var Data_netWorthTrend = '+json.dumps(points)+';'+chr(10)+'var Data_ACWorthTrend = '+json.dumps(cumulative)+';'
        return _capture(url,source_id,text.encode("utf8"),"application/javascript",at)
    if source_id == "cn_state_council":
        import test_pipeline_schema4 as original
        text = '<meta name="PubDate" content="2020-01-01"><title>Explicit engineering source</title><div id="UCAP-CONTENT">'+original.NEWS_BODY+ECONOMIC_QUOTE+'</div>'
        return _capture(url, source_id, text.encode("utf8"), "text/html", at)
    if source_id == "csi_index_identity" and url == IDENTITY_URL:
        return _capture(url, source_id, _identity_pdf(), "application/pdf", at)
    if source_id == "issuer_cmfchina" and re.search(r"/fundarticle/holdings_[0-9]{6}\.html$", url):
        code = re.search(r"holdings_([0-9]{6})", url).group(1)
        first, _ = _span()
        text = ("<html><body><p>合成原始基金披露，仅用于隔离测试。基金代码："+code+
                " 报告期末："+first.isoformat()+"</p><table><tr><th>行业类别</th><th>占基金资产净值比例</th></tr>"
                "<tr><td>黄金</td><td>80%</td></tr></table></body></html>")
        return _capture(url, source_id, text.encode("utf8"), "text/html", at)
    if source_id == "eastmoney_index_daily":
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        start = dt.datetime.strptime(query["beg"][0], "%Y%m%d").date()
        end = dt.datetime.strptime(query["end"][0], "%Y%m%d").date()
        day = min(instant(at).astimezone(CN).date(), end)
        first = max(start, day-dt.timedelta(days=3))
        prices = [(first+dt.timedelta(days=offset)).isoformat()+",100,100,100,100,0,0"
                  for offset in range((day-first).days+1) if (first+dt.timedelta(days=offset)).weekday() < 5]
        # Four-day acquisition is the declared synthetic observation process.
        # Every close becomes available at this real virtual capture, never
        # its earlier economic date. No future close appears in the response.
        body = json.dumps({"rc": 0, "data": {"code": "000300", "market": 1, "name": INDEX_NAME,
            "klines": prices}}).encode("utf8")
        return _capture(url, source_id, body, "application/json", at)
    return None


@contextlib.contextmanager
def _clock(fixture, at):
    import fund_universe
    import news
    import pipeline
    import stage_validation
    import state_store
    import validation_runtime
    modules = [contracts, industry_data, news, pipeline, stage_validation, state_store,
               validation_runtime, fund_universe, source_documents, sys.modules[fixture.__class__.__module__]]
    span,real_datetime = _span(),dt.datetime
    class CaptureClock(real_datetime):
        @classmethod
        def now(cls,tz=None):
            value = real_datetime.fromisoformat(at)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(sys.modules[__name__],"_span",return_value=span))
        stack.enter_context(patch.object(dt,"datetime",CaptureClock))
        for module in modules:
            if hasattr(module, "utc_now"):
                stack.enter_context(patch.object(module, "utc_now", return_value=at))
        yield


def seed_archives(fixture, *, capture_offsets=None):
    """Commit actual producer archives before the current public analysis."""
    module = sys.modules[fixture.__class__.__module__]
    base_capture = module.source_capture
    first, end = _span()
    initial_at = dt.datetime.combine(first, dt.time(8), CN).isoformat()
    until = (instant(initial_at)+dt.timedelta(days=400)).isoformat()
    if fixture.policy["max_age_seconds"] < 400*86400:
        raise ValueError("Synthetic archive fixture must explicitly declare its 400-day finite-input review policy")

    def archive_transport(url, source_id, **kwargs):
        at = contracts.utc_now()
        if source_id == "eastmoney_catalog":
            text = "var r = "+json.dumps([[code, "SYNTHETIC", "Synthetic constant NAV fixture", "指数型-其他", "SYNTHETIC"]
                                         for code in (module.CODE, module.raw_fixture.OTHER_CODE)])+";"
            return _capture(url, source_id, text.encode("utf8"), "application/javascript", at)
        result = extra_capture(url, source_id, observed_at=at, **kwargs)
        if result is not None:
            return result
        original = base_capture(url, source_id, **kwargs)
        # Rebuild the synthetic transport envelope at the virtual response
        # time. The production collector alone records first availability.
        return _capture(url, source_id, original["raw_bytes"], original["content_type"], at)

    with patch.object(source_fetch, "fetch", side_effect=archive_transport):
        with _clock(fixture, initial_at):
            fixture.call("archive-analysis", "analysis_start", {"news_policy": fixture.policy})
            collected = fixture.call("archive-news", "news_collect", {"run_id": "synthetic:archive-analysis",
                "cutoff_at": initial_at, "window_start": "2020-01-01T00:00:00Z",
                "sources": [{"source_id": "cn_state_council", "urls": [module.NEWS_URL]}],
                "required_source_groups": fixture.policy["required_source_groups"]})
            version = collected["versions"][0]
            claim = {"id": "synthetic-claim", "kind": "fact", "text": "Synthetic source quote contains a gold research statement",
                "version_id": version["version_id"], "quote": module.NEWS_BODY[:25], "categories": ["黄金"],
                "direction": "increase", "counterevidence": [{"missing_reason": "Explicit synthetic source world"}],
                "reviewer": "synthetic-test", "review_method": "fixture_quote_consistency"}
            event = {"event_key": None, "version_ids": [version["version_id"]], "claim_ids": ["synthetic-claim"],
                "event_at": None, "effective_from": None, "effective_until": None, "review_by": until,
                "supersedes": [], "retracts": [], "temporal_evidence": []}
            thesis = {"thesis_id": "synthetic-gold", "sector_id": "黄金", "kind": "asset_class", "label": "黄金", "direction": "increase",
                "horizon_days": 3, "search_terms": ["黄金"], "claim_ids": ["synthetic-claim"],
                "event_ids": [version["document_id"]], "required_source_groups": list(fixture.policy["required_source_groups"]),
                "valid_until": until, "event_basis": "standing_policy"}
            reviewed = fixture.call("archive-assessment", "news_assess", {"collection_id": "synthetic:archive-news",
                "run_id": "synthetic:archive-analysis", "claims": [claim], "industry_theses": [thesis], "events": [event],
                "economic_observations": [{"event_id": version["document_id"], "sector_ids": ["黄金"], "category": "industry_policy",
                    "metric_id": "policy_action", "metric_label": "黄金行业政策", "current": None, "prior": None, "expectation": None,
                    "action": {"version_id": version["version_id"], "quote": ECONOMIC_QUOTE, "value": "introduced"}}]})
            identity = fixture.call("archive-index-identity", "source_capture", {"url": IDENTITY_URL, "source_id": "csi_index_identity"})
            contract = {"sector_id": "黄金", "thesis_ids": ["synthetic-gold"], "benchmark_id": "synthetic-gold-index",
                "index_code": "000300", "index_name": INDEX_NAME, "provider_security_id": "1.000300",
                "return_definition": "price_return", "currency": "CNY", "calendar_id": "CN_A_SHARE",
                "timezone": "Asia/Shanghai", "identity_quote": "SyntheticGold 000300 CNY",
                "benchmark_role": "sector_index", "sector_relation": {"locator": "pdf/page/1/line/3",
                    "quote": SECTOR_RELATION_QUOTE, "sector_label": "黄金"},
                "identity_document_ref": identity["document_ref"], "start": first.isoformat(), "end": end.isoformat(),
                "asset_domain": {"kind": "equity", "role": "single_sector",
                    "return_target": {"transform": "price_log_return", "economic_meaning": "黄金行业的市场表现",
                        "source_unit": "index_point", "canonical_unit": "index_point", "locator": "pdf/page/1/line/3", "quote": SECTOR_RELATION_QUOTE},
                    "taxonomy": {"id": "SyntheticTaxonomy", "version": "v1", "locator": "pdf/page/1/line/4",
                        "quote": TAXONOMY_QUOTE, "mappings": [{"source_label": "黄金", "entity_id": "黄金", "role": "single_sector"}]}}}
            disclosures = []
            for code in (module.CODE, module.raw_fixture.OTHER_CODE):
                url = holdings_url(code)
                captured = fixture.call("archive-holdings-"+code, "source_capture", {"url": url, "source_id": "issuer_cmfchina"})
                disclosures.append({"code": code, "source_id": "issuer_cmfchina", "url": url, "role": "holdings",
                    "locators": [block["locator"] for block in captured["document"]["blocks"]]})
            fixture.call("archive-discovery", "fund_discover", {"news_review_id": "synthetic:archive-assessment",
                "news_policy": fixture.policy, "universe_policy": fixture.spec["universe_policy"],
                "monitoring_codes": [module.CODE], "issuer_disclosures": disclosures})

        store = Store(fixture.root, fixture.plan)
        review = {**fixture.artifacts.read_json(reviewed["manifest_ref"]), "manifest_ref": reviewed["manifest_ref"]}
        offsets = list(range(3,320,4)) if capture_offsets is None else list(capture_offsets)
        for offset in offsets:
            day = first+dt.timedelta(days=offset)
            at = dt.datetime.combine(day, dt.time(16), CN).isoformat()
            operation = "synthetic:archive-index-"+day.isoformat()
            request = {"kind": "synthetic_source_capture_archive", "decision_at": at, "benchmark_contracts": [contract]}
            with _clock(fixture, at):
                store.begin(operation, request)
                with store.lease(operation) as completed:
                    if completed is not None:
                        continue
                    result = industry_data.prepare_sector_data(review, fixture.spec, store, fixture.artifacts, operation, [contract])
                    store.complete(operation, result)
                if offset >= 123:
                    identity = "archive-nav-discovery-"+day.isoformat()
                    fixture.call(identity,"fund_discover",{
                        "news_review_id":"synthetic:archive-assessment","news_policy":fixture.policy,
                        "universe_policy":fixture.spec["universe_policy"],"monitoring_codes":[module.CODE],
                        "issuer_disclosures":disclosures})
                    fixture.call("archive-nav-"+day.isoformat(),"research_prepare",{
                        "discovery_id":"synthetic:"+identity,"start":first.isoformat(),"end":day.isoformat(),
                        "horizons":[3],"lookback":120})
        return {"benchmark_contracts": [contract], "issuer_disclosures": disclosures,
                "archive_review_id": "synthetic:archive-assessment", "archive_capture_count": len(offsets),
                "price_span_calendar_days": 320, "acquisition_interval_calendar_days": 4,
                "fund_nav_archive_capture_count":sum(offset>=123 for offset in offsets),"source_scope":"explicit_synthetic_virtual_source_history_not_market_PIT_evidence"}
