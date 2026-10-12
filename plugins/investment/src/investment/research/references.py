"""研究运行引用：准确媒体、命名空间、表元信息和角色只读范围。"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from investment.chains.news.contracts import NewsResolver
from investment.contracts import ContractError, SPEC, artifact, canonical, digest, require
from investment.references import safe_path
from investment.research.contracts import STAGES, guard_namespace

MEDIA_SUFFIX = {'application/json': '.json', 'text/plain': '.txt', 'text/markdown': '.md'}
ARTIFACT_FIELDS = ('artifact_id', 'schema_id', 'schema_version', 'media_type', 'uri', 'sha256', 'byte_size')
RUNTIME_FILES = {'input.json', 'method.json', 'candidate.json', 'receipt.json', 'sealed.json', 'failure.json'}


def selected_references(value, pointer):
    """单对象内按登记JSON Pointer取引用；数组星号不意味着递归授权。"""
    nodes = [value]
    for token in pointer.split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        selected = []
        for node in nodes:
            if token == '*' and isinstance(node, list):
                selected.extend(node)
            elif isinstance(node, dict) and token in node:
                selected.append(node[token])
            elif isinstance(node, list) and token.isdigit() and str(int(token)) == token and int(token) < len(node):
                selected.append(node[int(token)])
        nodes = selected
    return [node for node in nodes if node is not None]


class ResearchResolver:
    """只读已安装规范和准确运行文件，旧SC01/02继续使用其只读边界。"""

    def __init__(self, root, schemas, *, mode, allowed_refs=None, reference_grants=()):
        self.root = guard_namespace(root, mode)
        self.schemas, self.mode = schemas, mode
        self.allowed = None if allowed_refs is None else {}
        self.reference_grants = {item['schema_id']: item['reference_paths'] for item in reference_grants}
        self.local_context = None
        self.host_output_refs = []
        self.opaque_refs = set()
        self.legacy = NewsResolver(self.root, schemas, mode=mode, network_origin='live')
        for reference in allowed_refs or ():
            self.allow(reference)

    def allow(self, reference):
        self.schemas.validate_type('DatasetRef' if 'row_count' in reference else 'ArtifactRef', reference)
        if self.allowed is not None:
            previous = self.allowed.get(reference['uri'])
            same = previous is not None and all(previous[key] == reference[key] for key in ARTIFACT_FIELDS)
            require(previous is None or previous == reference or same and ('row_count' not in previous or 'row_count' not in reference),
                    'RS02', '/uri', 'Conflicting allowed reference')
            self.allowed[reference['uri']] = reference

    def _local_path(self, uri, schema_id):
        if self.local_context is None or schema_id not in self.schemas.targets:
            return None
        stage_id, role, directory = self.local_context
        parsed = urlsplit(uri)
        if parsed.scheme != 'investment-run' or parsed.netloc != stage_id.lower():
            return None
        _, parts = self.run_parts(uri)
        if len(parts) != 4 or parts[-1] in RUNTIME_FILES or parts[-1].startswith(('host-', 'producer-', 'validator-')):
            return None
        if role == 'producer' and parts[-1].startswith('validation-'):
            return None
        target = self.schemas.targets[schema_id]['target']
        if not parsed.path.endswith(MEDIA_SUFFIX[self.schemas.targets[schema_id]['media_type']]):
            return None
        owned = any(item['schema_id'] == schema_id for item in self.schemas.stage_registries[stage_id]['schemas'])
        if not owned or not target.startswith('urn:investment:spec:v1:' + stage_id.lower() + '#/'):
            return None
        path = self._location(uri)
        if path is None or path.resolve().parent != directory.resolve() or not directory.resolve().is_relative_to(self.root / 'runs'):
            return None
        return path

    def _admit_local(self, reference):
        self._media(reference)
        path = self._local_path(reference['uri'], reference['schema_id'])
        if path is None:
            return False
        raw = path.read_bytes()
        require(len(raw) == reference['byte_size'] and digest(raw) == reference['sha256'],
                'RS02', '/sha256', 'Current stage artifact hash or size differs', 'invalid_reference')
        if reference['media_type'] == 'application/json':
            self.schemas.document(reference['schema_id'], raw)
        else:
            self.schemas.validate(reference['schema_id'], raw.decode('utf-8'))
        self.allow(reference)
        return True

    def run_parts(self, uri):
        parsed = urlsplit(uri)
        parts = parsed.path.lstrip('/').split('/')
        require(not any(token in uri for token in ('%', '\\', '..', '?', '#'))
                and parsed.scheme == 'investment-run' and parsed.netloc.upper() in STAGES
                and parsed.netloc == parsed.netloc.lower() and not parsed.path.startswith('//')
                and len(parts) in (3, 4) and re.fullmatch(r'\d{8}', parts[0]) is not None
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', parts[1]) is not None,
                'RS01', '/uri', 'Invalid research run locator', 'invalid_reference')
        require(len(parts) == 3 or parts[2] in ('attempt-01', 'attempt-02', 'attempt-03'),
                'RS01', '/uri', 'Invalid attempt locator', 'invalid_reference')
        require(re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(json|md|txt)', parts[-1]) is not None,
                'RS01', '/uri', 'Invalid artifact filename', 'invalid_reference')
        try:
            datetime.strptime(parts[0], '%Y%m%d')
        except ValueError as exc:
            raise ContractError('RS01', '/uri', 'Invalid execution date', 'invalid_reference') from exc
        return parsed.netloc, parts

    def _location(self, uri):
        require(not any(token in uri for token in ('%', '\\', '..', '?', '#')),
                'RS01', '/uri', 'Forbidden locator syntax', 'invalid_reference')
        parsed = urlsplit(uri)
        if parsed.scheme == 'investment-run' and parsed.netloc.upper() in STAGES:
            stage, parts = self.run_parts(uri)
            return safe_path(self.root, 'runs', stage, *parts)
        if parsed.scheme == 'investment-spec':
            name = parsed.path.removeprefix('/')
            allowed = {'registry-research.json', 'method-research.json', 'research.schema.json'}
            for stage in self.schemas.stage_methods.keys() | self.schemas.stage_registries.keys():
                allowed.update((stage.lower() + '.schema.json', 'registry-' + stage.lower() + '.json', 'method-' + stage.lower() + '.json'))
            if name in allowed:
                require(parsed.netloc == 'v1' and parsed.path == '/' + name,
                        'RS01', '/uri', 'Noncanonical spec locator', 'invalid_reference')
                return SPEC / name
        return None

    def read(self, uri):
        if self.allowed is not None:
            require(uri in self.allowed, 'RS06', '/uri', 'Reference is outside role read scope', 'use_not_allowed')
        path = self._location(uri)
        try:
            return path.read_bytes() if path is not None else self.legacy.read(uri)
        except OSError as exc:
            raise ContractError('RS02', '/uri', 'Reference is unreadable', 'invalid_reference') from exc

    def path_ref(self, path, schema_id):
        selected = Path(path).resolve()
        require(selected.is_relative_to(self.root / 'runs'), 'RS01', '/path', 'Artifact outside selected run root', 'use_not_allowed')
        parts = selected.relative_to(self.root / 'runs').parts
        uri = 'investment-run://' + parts[0] + '/' + '/'.join(parts[1:])
        if parts[0].upper() in STAGES:
            self.run_parts(uri)
        require(schema_id in self.schemas.targets, 'RS02', '/schema_id', 'Unregistered schema')
        if self.allowed is not None and uri not in self.allowed:
            path = self._local_path(uri, schema_id)
            require(path is not None, 'RS06', '/uri', 'Path is outside current role artifact scope', 'use_not_allowed')
            raw = path.read_bytes()
        else:
            raw = self.read(uri)
        reference = artifact(raw, schema_id, uri)
        reference['media_type'] = self.schemas.targets[schema_id]['media_type']
        self._media(reference)
        if self.allowed is not None and uri not in self.allowed:
            self._admit_local(reference)
        return reference

    def _media(self, reference):
        sid = reference['schema_id']
        require(sid in self.schemas.targets and reference['schema_version'] == 1,
                'RS02', '/schema_id', 'Unregistered reference type/version', 'invalid_reference')
        media = self.schemas.targets[sid]['media_type']
        require(reference['media_type'] == media and urlsplit(reference['uri']).path.endswith(MEDIA_SUFFIX[media]),
                'RS02', '/media_type', 'Reference media or suffix mismatch', 'invalid_reference')

    def resolve(self, reference, expected=None, binding_only=False):
        self.schemas.validate_type('DatasetRef' if 'row_count' in reference else 'ArtifactRef', reference)
        require(expected is None or reference['schema_id'] == expected, 'RS02', '/schema_id', 'Reference role mismatch', 'identity_mismatch')
        self._media(reference)
        if self.allowed is not None:
            if self.allowed.get(reference['uri']) != reference:
                self._admit_local(reference)
            require(self.allowed.get(reference['uri']) == reference, 'RS06', '/uri', 'Reference differs from role binding', 'use_not_allowed')
        raw = self.read(reference['uri'])
        require(len(raw) == reference['byte_size'] and digest(raw) == reference['sha256'],
                'RS02', '/sha256', 'Reference hash or size mismatch', 'invalid_reference')
        if binding_only:
            return raw
        if reference['media_type'] == 'application/json':
            value = self.schemas.document(reference['schema_id'], raw)
            if reference['schema_id'] == 'investment.research_schema_document':
                Draft202012Validator.check_schema(value)
        else:
            try:
                value = raw.decode('utf-8')
            except UnicodeError as exc:
                raise ContractError('RS02', '/media_type', 'Text is not UTF-8', 'schema_violation') from exc
            self.schemas.validate(reference['schema_id'], value)
        if 'row_count' in reference:
            self._table(reference, value)
        if reference['schema_id'] == 'investment.funding_plan':
            require(value['source_type'] != 'synthetic_fixture' or self.mode == 'test',
                    'RS01', '/source_type', 'Synthetic funding facts require the isolated test namespace', 'use_not_allowed')
        grants = () if reference['uri'] in self.opaque_refs else self.reference_grants.get(reference['schema_id'], ())
        for pointer in grants:
            for child in selected_references(value, pointer):
                require(isinstance(child, dict), 'RS06', '/reference_grants', 'Granted path is not a Reference', 'schema_violation')
                self.schemas.validate_type('DatasetRef' if 'row_count' in child else 'ArtifactRef', child)
                require(not child['uri'].rsplit('/', 1)[-1].startswith('host-'),
                        'RS06', '/reference_grants', 'Host private artifacts require explicit current-step binding', 'use_not_allowed')
                self.allow(child)
        return value

    def _registry_for(self, schema_id):
        registries = [(self.schemas.registry, 'investment.registry', 'registry.json'),
                      (self.schemas.news_registry, 'investment.sc02_registry', 'registry-sc02.json'),
                      (self.schemas.research_registry, 'investment.research_registry', 'registry-research.json')]
        registries += [(registry, 'investment.' + stage.lower() + '_registry', 'registry-' + stage.lower() + '.json')
                       for stage, registry in self.schemas.stage_registries.items()]
        matches = [(registry, sid, name, table) for registry, sid, name in registries
                   for table in registry['tables'] if table['schema_id'] == schema_id]
        require(len(matches) == 1, 'RS02', '/schema_id', 'Unknown or ambiguous dataset registry')
        return matches[0]

    def _table(self, reference, value):
        registry, sid, name, table = self._registry_for(reference['schema_id'])
        table_ref = reference['table_schema_ref']
        require(table_ref['uri'] == 'investment-spec://v1/' + name, 'RS02', '/table_schema_ref', 'Installed registry required')
        self.allow(table_ref)
        require(self.resolve(table_ref, sid) == registry, 'RS02', '/table_schema_ref', 'Registry differs from installed rules')
        for key in ('primary_key', 'units', 'null_policy'):
            require(reference[key] == table[key], 'RS02', '/' + key, 'Table metadata mismatch')
        require(reference['row_count'] == len(value['rows']), 'RS02', '/row_count', 'Row count mismatch')
        keys = [tuple(canonical(row[key]) for key in table['primary_key']) for row in value['rows']]
        require(len(keys) == len(set(keys)), 'RS02', '/rows', 'Duplicate table key')
        for row in value['rows']:
            for column, units in table.get('row_units', {}).items():
                require(column in row and all(field in row for field in units.values()),
                        'RS02', '/row_units', 'Actual row lacks declared dynamic unit or currency field')

    def dataset_ref(self, value, schema_id, uri):
        registry, sid, name, table = self._registry_for(schema_id)
        self.schemas.validate(schema_id, value)
        reference = artifact(canonical(value), schema_id, uri)
        reference.update(table_schema_ref=artifact((SPEC / name).read_bytes(), sid, 'investment-spec://v1/' + name),
                         row_count=len(value['rows']), **{key: table[key] for key in ('primary_key', 'units', 'null_policy')})
        return reference
