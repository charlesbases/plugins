"""独立读取固定输入、受信 profile 与原始捕获，核对当前 SC02 候选。"""
from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from lxml import etree, html

from investment.contracts import ContractError, canonical, digest, instant, is_known, known, now, require, unknown
from investment.references import safe_path
from investment.chains.news.contracts import PENDING_DISPOSITIONS, REVIEW_USES, ROLES, RULES, NewsResolver, parse_news


def _url(value):
    parsed = urlsplit(value.strip())
    require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password,
            'N04', '/url', 'Invalid source HTTP URL')
    # 保留路径、参数顺序、重复参数及空参数；仅域名大小写与fragment不参与文档身份。
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', parsed.query, ''))


def _id(prefix, *parts):
    return prefix + '-' + digest(canonical(list(parts)))[:40]


def _header(capture, name):
    return [h['value'] for h in capture['response_headers'] if h['name'].lower() == name]


def _source_request_counts(captures, profile):
    """原生数据材料按冻结文档URL规则计入文档预算，避免由插件purpose改写预算。"""
    documents = [c for c in captures if c['purpose'] == 'document' or c['purpose'] == 'data'
                 and any(re.fullmatch(r['url_pattern'],c['final_url']) for r in profile.get('document_rules',[]))]
    document_ids = {c['capture_id'] for c in documents}
    pages = [c for c in captures if c['capture_id'] not in document_ids and c['purpose'] in ('entry','listing','data')]
    return len(pages),len(documents)


def _decode(capture, limit):
    raw = base64.b64decode(capture['response_bytes_base64'], validate=True)
    encodings = _header(capture, 'content-encoding')
    coding = ','.join(encodings).strip().lower()
    require(coding in ('', 'identity', 'gzip', 'deflate'), 'N06', '/raw_ref', 'Unsupported content encoding')
    if coding in ('gzip', 'deflate'):
        decoder = zlib.decompressobj(31 if coding == 'gzip' else 15)
        try:
            decoded = decoder.decompress(raw, limit + 1)
            require(len(decoded) <= limit and not decoder.unconsumed_tail, 'N05', '/raw_ref', 'Decoded byte limit')
            require(decoder.eof and not decoder.unused_data and len(decoded) <= limit,
                    'N06', '/raw_ref', 'Incomplete or concatenated compressed entity')
        except zlib.error as exc:
            raise ContractError('N06', '/raw_ref', 'Invalid content encoding') from exc
    else:
        decoded = raw
    require(len(decoded) <= limit, 'N05', '/raw_ref', 'Decoded byte limit')
    return decoded


def _text(value):
    if isinstance(value, etree._Element):
        return ' '.join(' '.join(value.itertext()).split())
    return ' '.join(str(value).split())


def _json_pointer(value, locator):
    if not locator:
        return value
    require(locator.startswith('/'), 'N06', '/profile_ref', 'Invalid JSON pointer')
    for token in locator[1:].split('/'):
        token = token.replace('~1', '/').replace('~0', '~')
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _source_json(raw):
    def no_constant(value):
        raise ValueError('Nonfinite source JSON')
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result:
                raise ValueError('Ambiguous duplicate source JSON key')
            result[key] = value
        return result
    return json.loads(raw, parse_constant=no_constant, object_pairs_hook=pairs)


def _tree(raw, profile, fmt):
    if fmt == 'json' or fmt in ('odata', 'geojson'):
        return _source_json(raw.decode(profile['charset'], errors='strict'))
    if fmt in ('rss', 'atom', 'xml'):
        return etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    tree = html.fromstring(raw.decode(profile['charset'], errors='strict'))
    for node in tree.xpath('//script|//style|//noscript'):
        node.drop_tree()
    return tree


def _one(tree, locator, json_mode=False):
    if not locator:
        return None
    try:
        if json_mode:
            return _json_pointer(tree, locator)
        values = tree.xpath(locator)
        if not isinstance(values, list):
            return values
        return values[0] if len(values) == 1 else None
    except (KeyError, IndexError, ValueError, TypeError, etree.XPathError):
        return None


def _material(capture, raw_ref, profile, profile_ref, limit):
    rule = next((r for r in profile['document_rules'] if re.fullmatch(r['url_pattern'], capture['final_url'])), None)
    require(rule is not None and rule['format'] != 'unresolved', 'N06', '/body_ref', 'Uncalibrated document layout')
    raw = _decode(capture, limit)
    try:
        tree = _tree(raw, profile, rule['format'])
        title = _one(tree, rule['title_locator'], rule['format'] == 'json')
        node = _one(tree, rule['body_locator'], False) if rule['format'] != 'json' else _json_pointer(tree, rule['body_locator'])
        source_time = _one(tree, rule['time_locator'], rule['format'] == 'json')
        require(title is not None and node is not None, 'N06', '/body_ref', 'Required source locator absent/ambiguous')
        if rule['format'] == 'json':
            require(rule['body_locator'] == '', 'N06', '/body_ref', 'Native JSON material must preserve complete record')
            text = raw.decode(profile['charset'], errors='strict')
        elif rule['format'] == 'xml':
            text = etree.tostring(node, method='c14n', with_comments=True).decode('utf-8')
        else:
            text = _text(node)
        require(bool(_text(title)) and bool(text.strip()), 'N06', '/body_ref', 'Empty source title/body')
        body = {'source_id':capture['source_id'], 'capture_ref':raw_ref, 'profile_ref':profile_ref,
                'format':{'html':'html_text','xml':'xml_text','json':'json_record'}[rule['format']],
                'title':_text(title), 'text':text,
                'source_time':known(_text(source_time)) if source_time is not None and _text(source_time) else unknown('source_time_missing'),
                'title_locator':rule['title_locator'], 'body_locator':rule['body_locator'], 'time_locator':rule['time_locator'],
                'native_media_type':'application/geo+json' if rule['material_kind'] == 'event_record' else {'html':'text/html','xml':'application/xml','json':'application/json'}[rule['format']]}
        return body, rule
    except (UnicodeError, ValueError, TypeError, KeyError, etree.Error) as exc:
        raise ContractError('N06', '/body_ref', 'Source layout/encoding cannot be independently extracted') from exc


def _time_evidence(body, rule, raw_ref):
    if not is_known(body['source_time']):
        return unknown('source_time_missing')
    text = body['source_time']['value']
    try:
        if rule['time_format'] == 'rfc3339':
            if re.search(r'\.\d{7,}', text):
                return unknown('source_time_precision_unsupported')
            point = instant(text)
            precision = 'instant'
        elif rule['time_format'] == 'epoch_ms':
            require(re.fullmatch(r'-?\d+', text) is not None, 'N07', '/window_time', 'Invalid epoch milliseconds')
            point = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(text))
            precision = 'instant'
        else:
            if not is_known(rule['timezone']):
                return unknown('source_timezone_missing')
            formats = {'date_ymd':['%Y-%m-%d'], 'date_dmy':['%d/%m/%Y'], 'date_mdy':['%m/%d/%Y'],
                       'english_date':['%B %d, %Y','%d %B %Y','%d %b %Y']}
            values = []
            for fmt in formats.get(rule['time_format'], []):
                try:
                    values.append(datetime.strptime(text, fmt))
                except ValueError:
                    continue
            if not values:
                return unknown('source_time_format_unknown')
            point = values[0].replace(tzinfo=ZoneInfo(rule['timezone']['value']))
            precision = 'date'
        lower = point.astimezone(timezone.utc)
        upper = (point + timedelta(days=1)).astimezone(timezone.utc) if precision == 'date' else lower
        def iso(value):
            return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')
        return known({'lower_bound':iso(lower),'upper_bound':iso(upper),'precision':precision,
                      'timezone':'UTC' if precision == 'instant' else rule['timezone']['value'],'basis_ref':raw_ref})
    except (ValueError, OverflowError, ContractError):
        return unknown('source_time_format_unknown')


def _membership(evidence, window):
    if not is_known(evidence):
        return 'uncertain'
    value = evidence['value']
    lower, upper = instant(value['lower_bound']), instant(value['upper_bound'])
    start, end = instant(window['start_at']), instant(window['end_at'])
    require(lower <= upper, 'N07', '/window_time', 'Reversed source time interval')
    if lower == upper:
        return 'within' if start <= lower < end else 'outside'
    if upper <= start or lower >= end:
        return 'outside'
    return 'within' if start <= lower and upper <= end else 'uncertain'


def _listing_publication(text, source_format, raw_ref):
    """只解释已声明published字段的显式offset/GMT，原字面值仍在机会及raw中。"""
    try:
        if source_format == 'rss' and re.search(r'(?:[+-]\d{4}|GMT|UT|UTC)$',text):
            point = parsedate_to_datetime(text)
        elif source_format == 'odata' and re.search(r'(?:Z|[+-]\d{2}:\d{2})$',text) and not re.search(r'\.\d{7,}',text):
            point = instant(text)
        else:
            return unknown('publication_time_offset_or_format_missing')
        if point.tzinfo is None:
            return unknown('source_timezone_missing')
        bound = point.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
        return known({'lower_bound':bound,'upper_bound':bound,'precision':'instant','timezone':'UTC','basis_ref':raw_ref})
    except (ValueError,TypeError,OverflowError,ContractError):
        return unknown('source_time_format_unknown')


def _choose_publication(body_time, listing_times):
    """保留一致证据中最窄的已有界限；不能凭窗口选择冲突时点或补造新界限。"""
    supported = ([body_time] if is_known(body_time) else []) + [v for v in listing_times if is_known(v)]
    if not supported:
        return body_time
    def bounds(value):
        return instant(value['value']['lower_bound']),instant(value['value']['upper_bound'])
    def contains(outer,inner):
        lower,upper = bounds(outer);start,end = bounds(inner)
        if lower == upper:
            return start == end == lower
        return lower <= start < upper if start == end else lower <= start and end <= upper
    for value in sorted(supported,key=lambda v:bounds(v)[1]-bounds(v)[0]):
        if all(contains(other,value) for other in supported):
            return value
    return unknown('conflicting_publication_evidence')


def _discover(captures, profiles, limit):
    opportunities, next_urls, recognized, layout_unknown = {}, {}, set(), set()
    publication_times = {}
    for raw_ref, capture in captures:
        sid = capture['source_id']
        next_urls.setdefault(sid, set())
        profile = profiles.get(sid)
        if profile is None or capture['purpose'] not in ('entry','listing','data') or capture['capture_state'] != 'complete' or not is_known(capture['http_status']) or not 200 <= capture['http_status']['value'] < 300:
            continue
        try:
            raw = _decode(capture, limit)
        except ContractError:
            layout_unknown.add(capture['capture_id'])
            continue
        for follow in profile['entry_follow_rules']:
            if not re.fullmatch(follow['url_pattern'], capture['final_url']):
                continue
            try:
                if follow['locator_kind'] == 'regex':
                    values = re.findall(follow['locator'], raw.decode(profile['charset'], errors='strict'))
                else:
                    tree = _tree(raw, profile, 'rss' if raw.lstrip().startswith(b'<?xml') else 'html')
                    values = tree.xpath(follow['locator'])
            except (ValueError,UnicodeError,etree.Error):
                layout_unknown.add(capture['capture_id'])
                continue
            for value in values:
                next_urls[sid].add(_url(urljoin(capture['final_url'], follow['link_prefix'] + _text(value))))
        for rule in profile['listing_rules']:
            if not re.fullmatch(rule['url_pattern'], capture['final_url']) or rule['format'] == 'unresolved':
                continue
            json_mode = rule['format'] in ('odata','geojson')
            try:
                tree = _tree(raw, profile, rule['format'])
                items = _json_pointer(tree, rule['items_locator']) if json_mode else tree.xpath(rule['items_locator'])
                require(isinstance(items, list), 'N08', '/opportunity_inventory', 'Listing container format changed')
            except (ContractError,ValueError,UnicodeError,KeyError,TypeError,etree.Error):
                layout_unknown.add(capture['capture_id'])
                continue
            # 零条仅说明本响应未暴露机会，完整空窗口另须独立窗口证明。
            recognized.add(capture['capture_id'])
            if rule['next_locator']:
                values = [_one(tree, rule['next_locator'], True)] if json_mode else tree.xpath(rule['next_locator'])
                for value in values:
                    if value:
                        next_urls[sid].add(_url(urljoin(capture['final_url'], _text(value))))
            for index, item in enumerate(items):
                value = _one(item, rule['url_locator'], json_mode)
                if value is None or not _text(value):
                    layout_unknown.add(capture['capture_id'])
                    continue
                url = _url(urljoin(capture['final_url'], rule['link_prefix'] + _text(value)))
                oid = _id('opp', sid, url)
                title, time = _one(item, rule['title_locator'], json_mode), _one(item, rule['time_locator'], json_mode)
                if rule['time_role'] == 'published' and time is not None and _text(time):
                    evidence = _listing_publication(_text(time),rule['format'],raw_ref)
                    values = publication_times.setdefault((sid,url),[])
                    if is_known(evidence) and evidence not in values:
                        values.append(evidence)
                locator = rule['items_locator'] + '/' + str(index) if json_mode else item.getroottree().getpath(item)
                row = opportunities.setdefault(oid, {'opportunity_id':oid,'source_id':sid,'url':url,
                    'material_kind':rule['material_kind'],'discovered_in':[],
                    'listed_title':known(_text(title)) if title is not None and _text(title) else unknown('listing_title_missing'),
                    'listed_time':known(_text(time)) if time is not None and _text(time) else unknown('listing_time_missing')})
                require(row['material_kind'] == rule['material_kind'], 'N08', '/opportunity_inventory', 'Conflicting source material kind')
                found = {'capture_ref':raw_ref,'locator':locator}
                if found not in row['discovered_in']:
                    row['discovered_in'].append(found)
    for row in opportunities.values():
        row['discovered_in'].sort(key=lambda v:(v['capture_ref']['uri'],v['locator']))
    return opportunities, next_urls, recognized, layout_unknown, publication_times


def _gap(rule, pointer, reason, evidence='source_bytes'):
    return {'rule_id':rule,'field_or_object':pointer,'reason':reason,'owner':'source',
            'required_evidence':[evidence],'affected_uses':REVIEW_USES}


def _bounded_window(profile, captures, window, limit):
    """仅承认固定规则能从本轮响应重读的明确有界目录声明。"""
    proof = profile['whole_window_proof']
    if not is_known(proof):
        return False
    rule = proof['value']
    for _,capture in captures:
        if capture['capture_state'] != 'complete' or not is_known(capture['http_status']) or not 200 <= capture['http_status']['value'] < 300 or not re.fullmatch(rule['url_pattern'],capture['final_url']):
            continue
        try:
            tree = _tree(_decode(capture,limit),profile,rule['format'])
            start = _one(tree,rule['start_locator'],rule['format'] == 'json')
            end = _one(tree,rule['end_locator'],rule['format'] == 'json')
            complete = _one(tree,rule['complete_locator'],rule['format'] == 'json')
            if (complete is True or _text(complete) == 'true') and start is not None and end is not None and instant(_text(start)) <= instant(window['start_at']) < instant(window['end_at']) <= instant(_text(end)):
                return True
        except (ContractError,ValueError,UnicodeError,etree.Error):
            continue
    return False


def _handoff(reference, resolver, run_id):
    context = resolver.resolve(reference, 'investment.transaction_context')
    parsed = urlsplit(reference['uri'])
    parts = parsed.path.strip('/').split('/')
    require(parsed.scheme == 'investment-run' and parsed.netloc == 'sc01' and len(parts) == 4
            and parts[-1] == 'context.json', 'N03', '/account_risk_objects_ref', 'Frozen SC01 context required')
    # 日期manifest为控制器记录，不伪装成普通SC01 run artifact。
    manifest = resolver.schemas.document('investment.daily_manifest',
                safe_path(resolver.root,'runs','sc01',parts[0],'manifest.json').read_bytes())
    matching = [v for v in manifest['invocations'] if v['invocation_id'] == parts[1]]
    require(len(matching) == 1 and matching[0]['run_id'] == run_id and matching[0]['processing_state'] == 'completed'
            and matching[0]['next_chain_allowed'] and is_known(matching[0]['sealed_output_ref']),
            'N03', '/account_risk_objects_ref', 'SC01 handoff is not completed for this round')
    sealed = resolver.resolve(matching[0]['sealed_output_ref']['value'], 'investment.sealed_output')
    candidate = resolver.resolve(sealed['candidate_output_ref'], 'investment.candidate_output')
    receipt = resolver.resolve(sealed['validation_receipt_ref'], 'investment.validation_receipt')
    require(sealed['stage_id'] == candidate['stage_id'] == receipt['stage_id'] == 'SC01'
            and sealed['run_id'] == candidate['run_id'] == receipt['run_id'] == run_id
            and candidate['payload_ref'] == reference and receipt['candidate_output_ref'] == sealed['candidate_output_ref']
            and receipt['overall_verdict'] in ('passed','partial') and receipt['business_result'] in ('usable','observation_only')
            and 'reporting' in receipt['allowed_uses'], 'N03', '/account_risk_objects_ref', 'SC01 receipt/context identity mismatch')
    return context


def validate_output(input_ref: dict, candidate_ref: dict, resolver: NewsResolver, *,
                    run_id: str, invocation_id: str, attempt_no: int) -> dict:
    """返回公共 ValidationReceipt；只读、不抓网、不修正输入或候选。"""
    checks = [{'rule_id':r,'verdict':'not_evaluated','reason':'dependency_not_evaluated','evidence_refs':[]} for r in RULES]
    receipt = {'validator_id':'sc02-validator-v1','validator_version':'v1','run_id':run_id,'stage_id':'SC02',
               'input_refs':[{'role':'frozen_input_ref','ref':input_ref}], 'candidate_output_ref':candidate_ref,
               'validated_at':now(),'checks':checks,'overall_verdict':'failed','business_result':'rejected',
               'allowed_uses':[],'blockers':[]}
    current = 'N01'
    def passed(rule):
        check = checks[RULES.index(rule)]
        check.update(verdict='passed', reason='integrity_supported', evidence_refs=[input_ref,candidate_ref])
    try:
        incoming = resolver.resolve(input_ref, 'investment.stage_input')
        candidate = resolver.resolve(candidate_ref, 'investment.candidate_output')
        receipt['input_refs'] = incoming['input_refs']
        require(incoming['stage_id'] == candidate['stage_id'] == 'SC02' and incoming['run_id'] == run_id
                and candidate['run_id'] == run_id and incoming['invocation_id'] == invocation_id and 1 <= attempt_no <= 3,
                current, '/run_id', 'SC02 invocation identity mismatch')
        passed(current); current = 'N02'
        input_parts, candidate_parts = resolver.run_parts(input_ref['uri']), resolver.run_parts(candidate_ref['uri'])
        require(candidate_parts == [input_parts[0],invocation_id,f'attempt-{attempt_no:02}','candidate.json'],
                current,'/candidate_output_ref','Candidate attempt locator mismatch')
        prefix = candidate_ref['uri'].rsplit('/',1)[0] + '/'
        corpus = resolver.resolve(candidate['payload_ref'], 'investment.evidence_corpus')
        names = ['documents','retrieval_attempts','opportunity_inventory','pending_items','extraction_spec','coverage']
        references = [corpus[n+'_ref'] for n in names]
        require(candidate['datasets'] == references, current,'/datasets','Corpus/dataset binding mismatch')
        require(candidate['payload_ref']['uri'] == prefix+'corpus.json'
                and all(r['uri'] == prefix+n+'.json' for n,r in zip(names,references)),
                current,'/datasets','Corpus/table locator mismatch')
        tables = {n:resolver.resolve(r,'investment.news_'+n)['rows'] for n,r in zip(names,references)}
        passed(current); current = 'N03'
        require([r['role'] for r in incoming['input_refs']] == [r[0] for r in ROLES], current,'/input_refs','Exact ordered input roles required')
        roles = {r['role']:r['ref'] for r in incoming['input_refs']}
        invocation_prefix = input_ref['uri'].rsplit('/',1)[0] + '/'
        for role,sid in ROLES:
            require(roles[role]['schema_id'] == sid, current,'/input_refs/'+role,'Role schema mismatch')
            if role != 'account_risk_objects_ref':
                require(roles[role]['uri'].startswith(invocation_prefix) and len(resolver.run_parts(roles[role]['uri'])) == 3,
                        current,'/input_refs/'+role,'Input artifact must be frozen in current invocation')
        clock = resolver.resolve(roles['decision_clock_ref'],'investment.decision_clock')
        window = resolver.resolve(roles['news_window_ref'],'investment.news_window')
        scope = resolver.resolve(roles['source_scope_ref'],'investment.source_scope')
        query = resolver.resolve(roles['query_protocol_ref'],'investment.query_protocol')
        parameters = resolver.resolve(incoming['parameters_ref'],'investment.sc02_parameters')
        require(parameters['mode'] == resolver.mode and parameters['network_origin'] == resolver.network_origin
                and Path(parameters['plugins_dir']).resolve() == resolver.plugins_dir,
                current,'/parameters_ref','Frozen parameters/CLI namespace mismatch')
        require(clock['decision_at'] == incoming['decision_at'] and clock['information_cutoff_at'] == incoming['information_cutoff_at']
                and instant(window['start_at']) < instant(window['end_at']) <= instant(incoming['information_cutoff_at']) <= instant(incoming['decision_at']),
                current,'/decision_clock_ref','Window/clock ordering mismatch')
        require(candidate['input_refs'] == incoming['input_refs'] and candidate['scope_ref'] == incoming['scope_ref'] == roles['source_scope_ref']
                and corpus['window_ref'] == roles['news_window_ref'] and corpus['source_scope_ref'] == roles['source_scope_ref']
                and corpus['corpus_id'] == 'corpus-'+input_ref['sha256'][:32], current,'/input_refs','Candidate changed fixed input/scope')
        method = resolver.resolve(candidate['method_ref'],'investment.sc02_method')
        require(method == resolver.schemas.news_method and candidate['method_ref']['uri'] == 'investment-spec://v1/method-sc02.json',
                current,'/method_ref','Untrusted method')
        account = _handoff(roles['account_risk_objects_ref'],resolver,run_id)
        require((account['source_authority'] == 'synthetic_fixture') == (resolver.mode == 'test'),
                current,'/account_risk_objects_ref','SC01 authority/root mismatch')
        invocation = resolver.frozen_invocation(input_ref,run_id,invocation_id)
        require(invocation['parameters_ref'] == incoming['parameters_ref'] and invocation['method_ref'] == candidate['method_ref']
                and incoming['parameters_ref']['uri'] == invocation_prefix+'parameters.json'
                and len(invocation['attempts']) == attempt_no, current,'/attempts','Frozen invocation metadata/current attempt mismatch')
        attempt = invocation['attempts'][attempt_no-1]
        require(is_known(attempt['candidate_ref']) and attempt['candidate_ref']['value'] == candidate_ref
                and is_known(attempt['capture_log_ref']), current,'/attempts','Candidate/capture ledger must be frozen before validation')
        entries = {e['source_id']:e for e in scope['entries']}
        require(len(entries) == len(scope['entries']) and list(entries) == sorted(entries),current,'/source_scope_ref','Unique ordered source entries required')
        bindings = invocation['profile_bindings']
        require([b['source_id'] for b in bindings] == list(entries),current,'/profile_bindings','Profile/source scope mismatch')
        profiles, manifests, profile_refs, extraction = {}, {}, {}, []
        for binding in bindings:
            sid = binding['source_id']; mr,pr = binding['manifest_ref'],binding['profile_ref']
            require(mr['status'] == pr['status'] and mr['status'] in ('known','unknown'),current,'/profile_bindings','Incomplete/unknown profile binding')
            if is_known(mr):
                config = resolver.source_config(input_ref,run_id,invocation_id,sid)
                manifest,profile = parse_news(config.manifest_json),parse_news(config.profile_json)
                profiles[sid],manifests[sid],profile_refs[sid] = profile,manifest,config.profile_ref
                state = profile['calibration_state']
            else:
                require(not (resolver.plugins_dir/sid).exists(),current,'/profile_bindings','Configured plugin cannot be silently omitted')
                state = 'missing'
            extraction.append({'source_id':sid,'manifest_ref':mr,'profile_ref':pr,'calibration_state':state})
        require(tables['extraction_spec'] == extraction,current,'/extraction_spec_ref','Frozen extraction table mismatch')
        passed(current)
        current = 'N04'
        log_ref = attempt['capture_log_ref']['value']
        require(log_ref['uri'] == prefix+'capture-log.json',current,'/capture_log_ref','Capture ledger locator mismatch')
        log = resolver.resolve(log_ref,'investment.news_capture_log')
        require(log['run_id'] == run_id and log['invocation_id'] == invocation_id and log['attempt_no'] == attempt_no
                and log['request_id'] == _id('batch',run_id,invocation_id,attempt_no),current,'/capture_log_ref','Capture ledger identity mismatch')
        captures = [(r,resolver.resolve(r,'investment.news_capture')) for r in log['captures']]
        require([c['sequence_no'] for _,c in captures] == list(range(1,len(captures)+1))
                and len({c['capture_id'] for _,c in captures}) == len(captures),current,'/captures','Capture IDs/sequences mismatch')
        by_id = {c['capture_id']:(r,c) for r,c in captures}
        retrieval_rows = []
        for raw_ref,capture in captures:
            sid = capture['source_id']
            require(sid in entries and raw_ref['uri'] == prefix+'capture-'+capture['capture_id']+'.json'
                    and capture['request_id'] == _id('req',run_id,invocation_id,attempt_no,sid),current,'/captures','Capture source/request binding mismatch')
            require(_url(capture['requested_url']) == _url(capture['final_url']),current,'/captures','Automatic unrecorded redirects forbidden')
            allowed_hosts = manifests[sid]['allowed_hosts'] if sid in manifests else [urlsplit(u).hostname for u in entries[sid]['entrypoints']]
            require(urlsplit(capture['final_url']).hostname in allowed_hosts,current,'/captures','Capture crossed source host boundary')
            require(capture['origin'] == ('synthetic' if resolver.network_origin == 'simulated' else 'live'),current,'/origin','Capture origin/namespace mismatch')
            raw = base64.b64decode(capture['response_bytes_base64'],validate=True)
            require(base64.b64encode(raw).decode() == capture['response_bytes_base64'] and len(raw) == capture['response_byte_size']
                    and digest(raw) == capture['response_sha256'],current,'/captures','Original response bytes/digest mismatch')
            require(instant(attempt['started_at']) <= instant(capture['request_started_at']) <= instant(capture['response_finished_at']) <= instant(log['closed_at'])
                    and instant(log['closed_at']) <= instant(candidate['created_at']),current,'/captures','Capture/candidate lifecycle times mismatch')
            require(is_known(capture['http_status']) or capture['capture_state'] == 'failed',current,'/captures','Response lacks actual HTTP status')
            require(capture['capture_state'] == 'complete' or is_known(capture['error']),current,'/captures','Partial/failure lacks error')
            if is_known(capture['parent_capture_id']):
                parent = by_id.get(capture['parent_capture_id']['value'])
                require(parent is not None and parent[1]['sequence_no'] < capture['sequence_no'] and parent[1]['source_id'] == sid
                        and instant(parent[1]['response_finished_at']) <= instant(capture['request_started_at'])
                        and is_known(parent[1]['redirect_target']) and _url(parent[1]['redirect_target']['value']) == _url(capture['requested_url']),
                        current,'/parent_capture_id','Redirect hop missing or mismatched')
            status = capture['http_status']['value'] if is_known(capture['http_status']) else None
            if status in (301,302,303,307,308):
                locations = _header(capture,'location')
                require(len(locations) == 1 and is_known(capture['redirect_target'])
                        and _url(capture['redirect_target']['value']) == _url(urljoin(capture['final_url'],locations[0])),
                        current,'/redirect_target','Redirect target lacks response support')
            else:
                require(not is_known(capture['redirect_target']),current,'/redirect_target','Nonredirect response claimed redirect')
            retrieval_rows.append({key:capture[key] for key in ('capture_id','request_id','source_id','sequence_no','purpose',
                                  'requested_url','final_url','request_started_at','response_finished_at','capture_state','origin')} | {'capture_ref':raw_ref})
        require(tables['retrieval_attempts'] == sorted(retrieval_rows,key=lambda r:r['capture_id']),current,'/retrieval_attempts_ref','Executed request ledger omitted/changed')
        require(candidate['evidence_refs'] == [log_ref]+log['captures'],current,'/evidence_refs','Candidate evidence must bind complete trusted capture ledger')
        passed(current); current = 'N05'
        limits = query['limits']
        require(limits['per_source_concurrency'] <= limits['global_concurrency'] and len(captures) <= limits['request_budget'],
                current,'/limits','Request/concurrency budget mismatch','resource_limit')
        require(all(c['response_byte_size'] <= limits['response_byte_limit'] for _,c in captures),current,'/captures','Response byte budget exceeded','resource_limit')
        for sid in entries:
            source_captures = [c for _,c in captures if c['source_id'] == sid]
            page_count,document_count = _source_request_counts(source_captures,profiles.get(sid,{}))
            require(page_count <= limits['page_budget'] and document_count <= limits['document_budget'],
                    current,'/limits','Source page/document budget exceeded','resource_limit')
            for capture in source_captures:
                depth,hop = 0,capture
                while is_known(hop['parent_capture_id']):
                    depth += 1;hop = by_id[hop['parent_capture_id']['value']][1]
                require(depth <= limits['redirect_budget'],current,'/redirect_budget','Redirect budget exceeded','resource_limit')
        intervals = sorted([(instant(c['request_started_at']),1,c['source_id']) for _,c in captures]
                           +[(instant(c['response_finished_at']),-1,c['source_id']) for _,c in captures])
        active,total = {s:0 for s in entries},0
        for _,delta,sid in intervals:
            total += delta;active[sid] += delta
            require(total <= limits['global_concurrency'] and active[sid] <= limits['per_source_concurrency'],
                    current,'/limits','Actual concurrency exceeded','resource_limit')
        for _,capture in captures:
            elapsed = (instant(capture['response_finished_at'])-instant(capture['request_started_at'])).total_seconds()*1000
            require(capture['capture_state'] != 'complete' or elapsed <= limits['timeout_ms'],current,'/timeout_ms','Completed response exceeded total timeout','resource_limit')
        passed(current)
        current = 'N06'
        discovered,next_urls,recognized,listing_unknown,publication_times = _discover(captures,profiles,limits['decoded_byte_limit'])
        capture_order = {r['uri']:c['sequence_no'] for r,c in captures}
        supported, unparsed = {}, set()
        for raw_ref,capture in captures:
            sid = capture['source_id']
            if sid not in profiles or capture['purpose'] not in ('document','data') or capture['capture_state'] != 'complete' or not is_known(capture['http_status']) or not 200 <= capture['http_status']['value'] < 300:
                continue
            try:
                body,rule = _material(capture,raw_ref,profiles[sid],profile_refs[sid],limits['decoded_byte_limit'])
            except ContractError:
                unparsed.add(capture['capture_id'])
                continue
            root_capture = capture
            publication_urls = {_url(capture['requested_url']),_url(capture['final_url'])}
            while is_known(root_capture['parent_capture_id']):
                root_capture = by_id[root_capture['parent_capture_id']['value']][1]
                publication_urls.update((_url(root_capture['requested_url']),_url(root_capture['final_url'])))
            document_id = _id('doc',manifests[sid]['publisher_id'],_url(capture['final_url']))
            version_id = 'ver-'+digest(_decode(capture,limits['decoded_byte_limit']))
            source_time = _time_evidence(body,rule,raw_ref)
            if rule['time_role'] == 'published':
                evidence = [v for url in sorted(publication_urls) for v in publication_times.get((sid,url),[])]
                evidence.sort(key=lambda v:capture_order[v['value']['basis_ref']['uri']])
                source_time = _choose_publication(source_time,evidence)
            membership = _membership(source_time,window)
            supported[capture['capture_id']] = {'capture':capture,'raw_ref':raw_ref,'body':body,'rule':rule,
                'requested_url':root_capture['requested_url'],'document_id':document_id,'version_id':version_id,
                'time':source_time,'membership':membership}
        primary_material = {}
        for item in supported.values():
            primary_material.setdefault((item['document_id'],item['version_id']),item)
        expected_documents = {}
        for document in tables['documents']:
            raw_ref = document['raw_ref']
            match = next((item for item in supported.values() if item['raw_ref'] == raw_ref),None)
            require(match is not None,current,'/documents/'+document['document_id'],'Document lacks supported original capture')
            require(primary_material[(match['document_id'],match['version_id'])]['raw_ref'] == raw_ref,
                    current,'/raw_ref','Duplicate version must retain earliest capture as primary')
            capture,body,rule = match['capture'],match['body'],match['rule']
            body_actual = resolver.resolve(document['body_ref'],'investment.news_body')
            require(document['body_ref']['uri'] == prefix+'body-'+capture['capture_id']+'.json' and body_actual == body,
                    current,'/body_ref','Body/title/locator differs from independent extraction','unsupported_output')
            expected = {'document_id':match['document_id'],'version_id':match['version_id'],
                'publisher_id':manifests[capture['source_id']]['publisher_id'],'source_id':capture['source_id'],
                'title':body['title'],'material_kind':rule['material_kind'],'requested_url':match['requested_url'],
                'final_url':capture['final_url'],'raw_ref':raw_ref,'body_ref':document['body_ref'],
                'published_time':match['time'] if rule['time_role'] == 'published' else unknown('not_publication_time'),
                'window_time':match['time'],'window_time_role':rule['time_role'],'window_membership':match['membership'],
                'available_time':known({'lower_bound':capture['response_finished_at'],'upper_bound':capture['response_finished_at'],
                    'precision':'instant','timezone':'UTC','basis_ref':raw_ref}) if instant(capture['response_finished_at']) <= instant(incoming['information_cutoff_at']) else unknown('no_availability_by_cutoff'),
                'retrieved_at':capture['response_finished_at'],
                'availability_basis':'observed_response' if instant(capture['response_finished_at']) <= instant(incoming['information_cutoff_at']) else 'unknown_before_cutoff',
                'capture_status':'complete'}
            expected_documents[(document['document_id'],document['version_id'])] = expected
        passed(current); current = 'N07'
        require(tables['documents'] == sorted(expected_documents.values(),key=lambda d:(d['document_id'],d['version_id'])),
                current,'/documents','Source time/identity/window/availability differs from original evidence','unsupported_output')
        require(all(d['window_membership'] != 'outside' and d['material_kind'] in entries[d['source_id']]['material_kinds'] for d in tables['documents']),
                current,'/documents','Out-of-window or excluded-kind document adopted')
        passed(current); current = 'N08'
        for sid,entry in entries.items():
            reachable = {_url(u) for u in entry['entrypoints']}
            edges = {}
            for raw_ref,capture in captures:
                if capture['source_id'] != sid:
                    continue
                found,follows,_,_,_ = _discover([(raw_ref,capture)],profiles,limits['decoded_byte_limit'])
                targets = set(follows.get(sid,set())) | {o['url'] for o in found.values()}
                if is_known(capture['redirect_target']):
                    targets.add(_url(capture['redirect_target']['value']))
                edges.setdefault(_url(capture['requested_url']),set()).update(targets)
            frontier = list(reachable)
            while frontier:
                for target in edges.get(frontier.pop(),set()) - reachable:
                    reachable.add(target);frontier.append(target)
            require(all(_url(c['requested_url']) in reachable for _,c in captures if c['source_id'] == sid),
                    current,'/captures','Request lacks a path from configured entry/discovery/pagination/redirect')
        opportunity_rows = {}
        for opportunity in tables['opportunity_inventory']:
            oid = opportunity['opportunity_id']
            require(oid in discovered,current,'/opportunity_inventory','Invented opportunity')
            expected = discovered[oid]
            require(all(opportunity[key] == value for key,value in expected.items()),current,'/opportunity_inventory/'+oid,'Discovered opportunity/source/locator changed')
            matches = [item for item in supported.values() if item['capture']['source_id'] == opportunity['source_id'] and _url(item['requested_url']) == opportunity['url']]
            adopted = [d for d in tables['documents'] if d['source_id'] == opportunity['source_id'] and _url(d['requested_url']) == opportunity['url']]
            if opportunity['material_kind'] not in entries[opportunity['source_id']]['material_kinds']:
                disposition,ids = 'excluded_kind',[]
            elif matches and all(m['membership'] == 'outside' for m in matches):
                disposition,ids = 'excluded_outside_window',[]
            elif any(m['membership'] != 'outside' for m in matches):
                expected_keys = {(m['document_id'],m['version_id']) for m in matches if m['membership'] != 'outside'}
                all_adopted = [d for d in tables['documents'] if (d['document_id'],d['version_id']) in expected_keys]
                require(expected_keys == {(d['document_id'],d['version_id']) for d in all_adopted},current,'/documents','Supported discovered material omitted')
                disposition = 'captured' if adopted else 'duplicate_version'
                ids = sorted({d['document_id'] for d in all_adopted})
            else:
                attempts = []
                for _,capture in captures:
                    root_capture = capture
                    while is_known(root_capture['parent_capture_id']):
                        root_capture = by_id[root_capture['parent_capture_id']['value']][1]
                    if capture['source_id'] == opportunity['source_id'] and _url(root_capture['requested_url']) == opportunity['url'] and capture['purpose'] in ('document','data'):
                        attempts.append(capture)
                disposition = 'pending_fetch' if not attempts else 'pending_parse' if any(c['capture_id'] in unparsed for c in attempts) else 'failed_fetch'
                ids = []
            require(opportunity['disposition'] == disposition and opportunity['document_ids'] == ids,
                    current,'/opportunity_inventory/'+oid,'Opportunity disposition/document complement mismatch')
            opportunity_rows[oid] = opportunity
        require(set(opportunity_rows) == set(discovered),current,'/opportunity_inventory','Actually discovered opportunity omitted')
        require(all(any(d['document_id'] in o['document_ids'] for o in opportunity_rows.values()) for d in tables['documents']),
                current,'/documents','Document not in configured discovery inventory')
        passed(current)
        current = 'N09'
        pending = [{'pending_id':_id('pending',o['opportunity_id']),'source_id':o['source_id'],
                    'opportunity_id':o['opportunity_id'],'reason_code':o['disposition'],
                    'required_evidence':'supported_layout' if o['disposition'] == 'pending_parse' else 'source_response',
                    'affected_uses':REVIEW_USES} for o in opportunity_rows.values() if o['disposition'] in PENDING_DISPOSITIONS]
        pending.sort(key=lambda p:p['pending_id'])
        require(tables['pending_items'] == pending,current,'/pending_items_ref','Pending set is not exact unresolved opportunity complement')
        passed(current); current = 'N10'
        coverage, expected_gaps = [],[]
        for sid,entry in entries.items():
            source_captures = [c for _,c in captures if c['source_id'] == sid]
            visited = sorted({u for u in entry['entrypoints'] if any(_url(c['requested_url']) == _url(u) for c in source_captures)})
            source_opps = [o for o in opportunity_rows.values() if o['source_id'] == sid]
            source_docs = [d for d in tables['documents'] if d['source_id'] == sid]
            source_pending = [p for p in pending if p['source_id'] == sid]
            errors = []
            def source_error(code,message,capture_id=None):
                errors.append({'source_id':sid,'code':code,'capture_id':known(capture_id) if capture_id else unknown('source_scope_gap'),'message':message})
            if sid not in profiles:
                source_error('unsupported_source','no_installed_profile')
            elif profiles[sid]['calibration_state'] == 'unresolved':
                source_error('unresolved_profile','profile_calibration_unresolved')
            for capture in source_captures:
                status = capture['http_status']['value'] if is_known(capture['http_status']) else None
                if capture['capture_state'] != 'complete' or status is None or status >= 400:
                    source_error('request_failed',capture['error']['value']['message'] if is_known(capture['error']) else 'HTTP_'+str(status),capture['capture_id'])
                if capture['capture_id'] in unparsed:
                    source_error('layout_unknown','layout_unknown',capture['capture_id'])
                if capture['capture_id'] in listing_unknown:
                    source_error('layout_unknown','listing_layout_unknown',capture['capture_id'])
            requested = {_url(c['requested_url']) for c in source_captures}
            outstanding = len(visited) < len(entry['entrypoints']) or bool(next_urls.get(sid,set()) - requested) or bool(source_pending)
            page_count,document_count = _source_request_counts(source_captures,profiles.get(sid,{}))
            budget = outstanding and (len(captures) >= limits['request_budget']
                    or page_count >= limits['page_budget'] or document_count >= limits['document_budget'])
            if budget:
                source_error('budget_stop','finite_resource_budget_stopped_scope')
            listing_failures = any(c['purpose'] in ('entry','listing','data') and
                  (c['capture_state'] != 'complete' or not is_known(c['http_status']) or c['http_status']['value'] >= 400) for c in source_captures)
            policies = [r['exhaustion_policy'] for r in profiles.get(sid,{}).get('listing_rules',[])]
            exposed = bool(recognized & {c['capture_id'] for c in source_captures}) and not outstanding
            listing_state = 'error' if listing_failures else 'budget_stop' if budget else 'exhausted_exposed' if exposed and policies and all(p in ('finite_exposed','paginated_unknown') for p in policies) else 'unknown'
            bounded = sid in profiles and _bounded_window(profiles[sid],[(r,c) for r,c in captures if c['source_id'] == sid],window,limits['decoded_byte_limit'])
            complete = bounded and exposed and not errors and all(d['window_membership'] == 'within' for d in source_docs)
            if bounded and not listing_failures:
                listing_state = 'verified_window_boundary'
            if not complete:
                source_error('coverage_unknown','whole_window_not_proven')
            status = 'unsupported' if sid not in profiles else 'completed' if complete else 'partial' if source_docs else 'failed'
            row = {'source_id':sid,'status':status,'listing_state':listing_state,'window_coverage':'complete' if complete else 'unknown',
                   'configured_entrypoints':entry['entrypoints'],'visited_entrypoints':visited,
                   'capture_ids':sorted(c['capture_id'] for c in source_captures),
                   'opportunity_ids':sorted(o['opportunity_id'] for o in source_opps),
                   'document_ids':sorted({d['document_id'] for d in source_docs}),
                   'pending_ids':sorted(p['pending_id'] for p in source_pending),'errors':errors,'reason':'whole_window_proven' if complete else 'whole_window_not_proven'}
            coverage.append(row)
            if not complete:
                expected_gaps.append(_gap('N10','/coverage/'+sid,'whole_window_not_proven'))
        require(tables['coverage'] == coverage,current,'/coverage_ref','Source/coverage sets or boundary claim differs from independent inventory')
        passed(current)
        if any(r['window_coverage'] != 'complete' for r in coverage):
            checks[RULES.index('N10')].update(verdict='insufficient_evidence',reason='whole_window_not_proven')
        current = 'N11'
        require(bool(tables['documents']) or all(r['window_coverage'] == 'complete' for r in coverage),
                current,'/documents','No valid material and no independently proven empty window','missing_evidence')
        for document in tables['documents']:
            if document['window_membership'] == 'uncertain':
                reason = 'conflicting_publication_evidence' if document['window_time']['reason'] == 'conflicting_publication_evidence' else 'window_membership_uncertain'
                expected_gaps.append(_gap('N07','/documents/'+document['document_id']+'/window_time',reason))
            if not is_known(document['available_time']):
                expected_gaps.append(_gap('N07','/documents/'+document['document_id']+'/available_time','no_availability_by_cutoff'))
        passed(current); current = 'N12'
        expected_gaps.sort(key=lambda g:(g['rule_id'],g['field_or_object']))
        require(candidate['gaps'] == expected_gaps,current,'/gaps','Candidate gap set is incomplete/unsupported')
        passed(current)
        receipt.update(overall_verdict='partial' if expected_gaps or resolver.network_origin == 'simulated' else 'passed',
                       business_result='observation_only' if expected_gaps or resolver.network_origin == 'simulated' else 'usable',
                       allowed_uses=REVIEW_USES,blockers=expected_gaps)
    except (ContractError,OSError,ValueError,KeyError,TypeError,etree.Error,binascii.Error) as error:
        rule = error.rule if isinstance(error,ContractError) and error.rule in RULES else current
        location = error.pointer if isinstance(error,ContractError) else '/'
        reason = error.reason if isinstance(error,ContractError) else type(error).__name__+': '+str(error)[:700]
        missing = isinstance(error,ContractError) and error.code == 'missing_evidence' and rule == 'N11'
        checks[RULES.index(rule)].update(verdict='insufficient_evidence' if missing else 'failed',reason=reason,evidence_refs=[input_ref,candidate_ref])
        receipt.update(business_result='blocked' if missing else 'rejected',blockers=[_gap(rule,location,reason)])
    resolver.schemas.validate('investment.validation_receipt',receipt)
    return receipt
