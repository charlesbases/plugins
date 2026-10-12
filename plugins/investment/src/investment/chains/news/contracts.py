"""SC02 契约与只读引用边界；不改变 SC01 注册表或账户业务。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from referencing import Resource

from investment.contracts import ContractError, SPEC, Schemas, artifact, canonical, digest, require
from investment.references import Resolver, safe_path
from investment.runtime.paths import production_root, validate_root
from investment.chains.news.protocol import FrozenSourceConfig

SOURCES = Path(__file__).resolve().parent / 'sources'
NEWS_SPEC = 'urn:investment:spec:v1:sc02'
ROLES = (
    ('decision_clock_ref', 'investment.decision_clock'),
    ('news_window_ref', 'investment.news_window'),
    ('source_scope_ref', 'investment.source_scope'),
    ('query_protocol_ref', 'investment.query_protocol'),
    ('account_risk_objects_ref', 'investment.transaction_context'),
)
RULES = tuple(f'N{n:02}' for n in range(1, 13))
REVIEW_USES = ['evidence_review', 'hypothesis_research', 'reporting']
PENDING_DISPOSITIONS = {'pending_fetch', 'pending_parse', 'failed_fetch'}


def parse_news(raw: bytes):
    """规范 JSON 允许较大的 Base64 捕获外壳，原生数值保留在正文字符串。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def integer(text):
        value = int(text)
        if text == '-0' or abs(value) > 9007199254740991:
            raise ValueError('Invalid integer')
        return value

    def no_float(text):
        raise ValueError('Floating JSON token forbidden')

    try:
        require(0 < len(raw) <= 50331648, 'N01', '/', 'JSON byte limit', 'schema_violation')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_int=integer,
                           parse_float=no_float, parse_constant=no_float)
        require(canonical(value) == raw, 'N01', '/', 'Noncanonical JSON', 'schema_violation')
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError('N01', '/', 'Invalid canonical JSON', 'schema_violation') from exc


class NewsSchemas(Schemas):
    """扩展公共资源和 SC02 专用注册项，保留基类 SC01 registry/method。"""

    def __init__(self):
        super().__init__()
        document = json.loads((SPEC / 'sc02.schema.json').read_bytes())
        Draft202012Validator.check_schema(document)
        self.resources = self.resources.with_resource(document['$id'], Resource.from_contents(document))
        self.news_registry = parse_news((SPEC / 'registry-sc02.json').read_bytes())
        self.news_method = parse_news((SPEC / 'method-sc02.json').read_bytes())
        for item in self.news_registry['schemas']:
            previous = self.targets.get(item['schema_id'])
            require(previous is None or previous == item, 'N01', '/schemas', 'Conflicting schema registration')
            self.targets[item['schema_id']] = item
        self.validate('investment.sc02_registry', self.news_registry)
        self.validate('investment.sc02_method', self.news_method)
        for item in self.news_registry['schemas']:
            self.resources.resolver().lookup(item['target'])

    def document(self, schema_id: str, raw: bytes):
        return self.validate(schema_id, parse_news(raw))


def guard_namespace(root: Path, mode: str, network_origin: str, plugins_dir: Path | None = None):
    """必须先于输入/插件读取、建目录、锁及数据库操作调用。"""
    require(mode in ('formal', 'test') and network_origin in ('live', 'simulated'),
            'N01', '/mode', 'Invalid run mode', 'use_not_allowed')
    selected = validate_root(root, fixture_mode=mode == 'test')
    require(network_origin != 'simulated' or mode == 'test', 'N01', '/network_origin',
            'Simulation requires isolated test root', 'use_not_allowed')
    plugins = Path(plugins_dir or SOURCES).resolve()
    test_parent = production_root() / 'test'
    allowed = not plugins.is_relative_to(test_parent) if mode == 'formal' else plugins == SOURCES.resolve() or plugins.is_relative_to(selected)
    require(allowed, 'N01', '/plugins_dir', 'Plugin directory crosses run namespace', 'use_not_allowed')
    return selected, plugins


def news_dataset(value: dict, schema_id: str, uri: str, schemas: NewsSchemas):
    table = next((t for t in schemas.news_registry['tables'] if t['schema_id'] == schema_id), None)
    require(table is not None, 'N01', '/schema_id', 'Unknown SC02 table')
    result = artifact(canonical(value), schema_id, uri)
    result.update(table_schema_ref=artifact((SPEC / 'registry-sc02.json').read_bytes(),
                  'investment.sc02_registry', 'investment-spec://v1/registry-sc02.json'),
                  row_count=len(value['rows']), **{k: table[k] for k in ('primary_key', 'units', 'null_policy')})
    return result


class NewsResolver(Resolver):
    """只读 SC02 frozen run/spec；SC01 引用仍由既有边界读取。"""

    def __init__(self, root: Path, schemas: NewsSchemas, *, mode: str, network_origin: str,
                 plugins_dir: Path | None = None):
        selected, self.plugins_dir = guard_namespace(root, mode, network_origin, plugins_dir)
        super().__init__(selected, schemas, fixture_mode=mode == 'test')
        self.mode, self.network_origin = mode, network_origin

    def run_parts(self, uri: str):
        parsed = urlsplit(uri)
        parts = parsed.path.lstrip('/').split('/')
        require(parsed.scheme == 'investment-run' and parsed.netloc == 'sc02' and not parsed.path.startswith('//') and not parsed.query
                and not parsed.fragment and len(parts) in (3, 4)
                and re.fullmatch(r'\d{8}', parts[0]) is not None
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', parts[1]) is not None,
                'N02', '/uri', 'Invalid SC02 run locator', 'invalid_reference')
        require(len(parts) == 3 or parts[2] in ('attempt-01', 'attempt-02', 'attempt-03'),
                'N02', '/uri', 'Invalid attempt locator', 'invalid_reference')
        require(re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*\.json', parts[-1]) is not None,
                'N02', '/uri', 'Invalid JSON artifact locator', 'invalid_reference')
        try:
            datetime.strptime(parts[0], '%Y%m%d')
        except ValueError as exc:
            raise ContractError('N02', '/uri', 'Invalid execution date', 'invalid_reference') from exc
        return parts

    def read(self, uri: str):
        parsed = urlsplit(uri)
        if parsed.scheme == 'investment-spec' and parsed.path.lstrip('/') in ('registry-sc02.json', 'method-sc02.json'):
            require(parsed.netloc == 'v1' and not parsed.query and not parsed.fragment,
                    'N02', '/uri', 'Invalid SC02 spec locator', 'invalid_reference')
            require(parsed.path in ('/registry-sc02.json','/method-sc02.json'),
                    'N02', '/uri', 'Noncanonical SC02 spec path', 'invalid_reference')
            return (SPEC / parsed.path.lstrip('/')).read_bytes()
        if parsed.scheme == 'investment-run' and parsed.netloc == 'sc02':
            try:
                return safe_path(self.root, 'runs', 'sc02', *self.run_parts(uri)).read_bytes()
            except OSError as exc:
                raise ContractError('N02', '/uri', 'SC02 reference unreadable', 'invalid_reference') from exc
        require(parsed.scheme == 'investment-spec' or parsed.scheme == 'investment-run' and parsed.netloc == 'sc01',
                'N02', '/uri', 'Only frozen SC01/SC02 or installed spec allowed', 'invalid_reference')
        return super().read(uri)

    def path_ref(self, path: Path, schema_id: str):
        resolved = Path(path).resolve()
        run_root = self.root / 'runs' / 'sc02'
        require(resolved.is_relative_to(run_root), 'N01', '/input',
                'SC02 entry must be in selected run namespace', 'use_not_allowed')
        uri = 'investment-run://sc02/' + resolved.relative_to(run_root).as_posix()
        self.run_parts(uri)
        return artifact(self.read(uri), schema_id, uri)

    def _resolve(self, reference, expected=None, binding_only=False):
        self.schemas.validate_type('DatasetRef' if 'row_count' in reference else 'ArtifactRef', reference)
        sid = reference['schema_id']
        require(expected is None or sid == expected, 'N02', '/schema_id', 'Reference role mismatch', 'identity_mismatch')
        require(sid in self.schemas.targets and reference['schema_version'] == 1
                and reference['media_type'] == 'application/json', 'N02', '/schema_id', 'Unsupported reference')
        raw = self.read(reference['uri'])
        require(len(raw) == reference['byte_size'] and digest(raw) == reference['sha256'],
                'N02', '/sha256', 'Reference hash/size mismatch', 'invalid_reference')
        if binding_only:
            return raw
        value = self.schemas.document(sid, raw)
        if 'row_count' in reference:
            news = sid.startswith('investment.news_')
            registry = self.resolve(reference['table_schema_ref'], 'investment.sc02_registry' if news else 'investment.registry')
            trusted = self.schemas.news_registry if news else self.schemas.registry
            require(registry == trusted, 'N02', '/table_schema_ref', 'Untrusted table registry')
            table = next((t for t in registry['tables'] if t['schema_id'] == sid), None)
            require(table is not None, 'N02', '/schema_id', 'Unknown table')
            for key in ('primary_key', 'units', 'null_policy'):
                require(reference[key] == table[key], 'N02', '/' + key, 'Table metadata mismatch')
            require(reference['row_count'] == len(value['rows']), 'N02', '/row_count', 'Row count mismatch')
            keys = [tuple(row[k] for k in table['primary_key']) for row in value['rows']]
            require(len(set(keys)) == len(keys), 'N02', '/rows', 'Duplicate primary key')
            if news:
                require(keys == sorted(keys), 'N02', '/rows', 'Noncanonical row order')
        return value

    def frozen_invocation(self, input_ref, run_id, invocation_id):
        parts = self.run_parts(input_ref['uri'])
        require(parts[1:] == [invocation_id, 'input.json'], 'N03', '/invocation_id', 'Frozen input locator mismatch')
        try:
            raw = safe_path(self.root, 'runs', 'sc02', parts[0], 'manifest.json').read_bytes()
        except OSError as exc:
            raise ContractError('N03', '/invocation_id', 'Daily manifest missing', 'missing_evidence') from exc
        manifest = self.schemas.document('investment.sc02_daily_manifest', raw)
        ids = [v['invocation_id'] for v in manifest['invocations']]
        require(manifest['execution_date'] == parts[0] and len(ids) == len(set(ids)),
                'N03', '/invocations', 'Daily manifest identity mismatch')
        selected = [v for v in manifest['invocations'] if v['invocation_id'] == invocation_id]
        require(len(selected) == 1 and selected[0]['run_id'] == run_id and selected[0]['frozen_input_ref'] == input_ref,
                'N03', '/invocation_id', 'Frozen invocation binding mismatch')
        record = selected[0]
        require([a['attempt_no'] for a in record['attempts']] == list(range(1, len(record['attempts']) + 1))
                and len(record['attempts']) <= 3, 'N03', '/attempts', 'Attempt budget/order mismatch')
        return record

    def source_config(self, input_ref: dict, run_id: str, invocation_id: str, source_id: str) -> FrozenSourceConfig:
        """从本调用profile_bindings解析受信规则，校验字节/类型/来源后提供只读视图。"""
        incoming = self.resolve(input_ref, 'investment.stage_input')
        require(incoming['run_id'] == run_id and incoming['invocation_id'] == invocation_id and incoming['stage_id'] == 'SC02',
                'N03','/profile_bindings','Source configuration invocation mismatch')
        invocation = self.frozen_invocation(input_ref,run_id,invocation_id)
        selected = [b for b in invocation['profile_bindings'] if b['source_id'] == source_id]
        require(len(selected) == 1 and selected[0]['manifest_ref']['status'] == selected[0]['profile_ref']['status'] == 'known',
                'N03','/profile_bindings','Frozen source configuration is unavailable','missing_evidence')
        scope_ref = next((r['ref'] for r in incoming['input_refs'] if r['role'] == 'source_scope_ref'),None)
        require(scope_ref is not None,'N03','/source_scope_ref','Source scope binding missing')
        prefix = input_ref['uri'].rsplit('/',1)[0]+'/'
        require(scope_ref == incoming['scope_ref'] and scope_ref['uri'].startswith(prefix)
                and len(self.run_parts(scope_ref['uri'])) == 3,
                'N03','/source_scope_ref','Source scope must be frozen in current invocation')
        scope = self.resolve(scope_ref,'investment.source_scope')
        require(sum(e['source_id'] == source_id for e in scope['entries']) == 1,
                'N03','/source_id','Source configuration is outside current request scope')
        binding = selected[0]
        manifest_ref,profile_ref = binding['manifest_ref']['value'],binding['profile_ref']['value']
        require(manifest_ref['uri'] == prefix+'plugin-'+source_id+'.json' and profile_ref['uri'] == prefix+'profile-'+source_id+'.json',
                'N03','/profile_bindings','Source rules must be frozen in current invocation')
        manifest_raw = self.resolve(manifest_ref,'investment.news_manifest',binding_only=True)
        profile_raw = self.resolve(profile_ref,'investment.news_profile',binding_only=True)
        manifest = self.schemas.document('investment.news_manifest',manifest_raw)
        profile = self.schemas.document('investment.news_profile',profile_raw)
        require(all((self.plugins_dir/source_id/name).resolve().is_relative_to(self.plugins_dir)
                    for name in ('manifest.json','extraction.json')),
                'N03','/profile_bindings','Source rule files escape trusted plugin directory')
        require(manifest_raw == (self.plugins_dir/source_id/'manifest.json').read_bytes()
                and profile_raw == (self.plugins_dir/source_id/'extraction.json').read_bytes()
                and manifest['source_id'] == profile['source_id'] == source_id
                and manifest['profile_sha256'] == profile_ref['sha256'],
                'N03','/profile_bindings','Frozen source rules differ from installed identity/bytes')
        self.schemas.validate('investment.collector_config',{'source_id':source_id,'manifest_ref':manifest_ref,
                    'profile_ref':profile_ref,'manifest':manifest,'profile':profile})
        return FrozenSourceConfig(source_id,canonical(manifest_ref),canonical(profile_ref),manifest_raw,profile_raw)
