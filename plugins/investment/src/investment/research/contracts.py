"""研究阶段注册与上下文；业务推导和独立科学验证由各子链负责。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import InitVar, dataclass, field
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from jsonschema import Draft202012Validator
from referencing import Resource

from investment.chains.news.contracts import NewsSchemas, parse_news
from investment.contracts import ContractError, SPEC, artifact, canonical, digest, instant, is_known, now, require
from investment.runtime.paths import validate_root
from investment.runtime.runs import atomic

STAGES = tuple(f'SC{number:02}' for number in range(3, 11))
PLUGIN = Path(__file__).resolve().parents[3]


def guard_namespace(root, mode):
    require(mode in ('formal', 'test'), 'RS01', '/mode', 'Unknown runtime mode', 'use_not_allowed')
    return validate_root(root, fixture_mode=mode == 'test')


def scientific_decimal(value):
    """研究估计可用指数文本，金额仍由公共精确十进制规则约束。"""
    if not isinstance(value, str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?', value):
        return False
    number = float(value)
    return math.isfinite(number) and not (number == 0 and (value.startswith('-') or Decimal(value) != 0))


class ResearchSchemas(NewsSchemas):
    """只扩展显式请求的安装资源，不下载Schema，不扫描未完成阶段。"""

    def __init__(self, stages=()):
        super().__init__()
        self.stage_methods, self.stage_registries, self.documents = {}, {}, {}
        self.installed_files, self.installed_hashes = {}, {}
        for name in ('common.schema.json', 'sc01.schema.json', 'storage.schema.json', 'runs.schema.json', 'sc02.schema.json'):
            raw = (SPEC / name).read_bytes()
            document = json.loads(raw)
            self.documents[document['$id']] = document
            self.installed_files[document['$id']] = name
            self.installed_hashes[document['$id']] = digest(raw)
        self.format_checker.checks('scientific-decimal')(scientific_decimal)
        self._resource('research.schema.json', 'urn:investment:spec:v1:research')
        self.research_registry = parse_news((SPEC / 'registry-research.json').read_bytes())
        self.research_method = parse_news((SPEC / 'method-research.json').read_bytes())
        self._register(self.research_registry)
        self.validate('investment.research_registry', self.research_registry)
        self.validate('investment.research_method', self.research_method)
        require(len(stages) == len(set(stages)), 'RS02', '/stages', 'Duplicate stage registration')
        # 先加载所有指定资源，允许显式声明的跨阶段类型引用。
        for stage_id in stages:
            require(stage_id in STAGES, 'RS02', '/stage_id', 'Unsupported research stage')
            name = stage_id.lower()
            self._resource(name + '.schema.json', 'urn:investment:spec:v1:' + name)
            registry = parse_news((SPEC / ('registry-' + name + '.json')).read_bytes())
            self._register(registry)
            self.stage_registries[stage_id] = registry
        for stage_id in stages:
            name = stage_id.lower()
            self.validate('investment.' + name + '_registry', self.stage_registries[stage_id])
            self._stage_registry(stage_id)
            method = self.document('investment.' + name + '_method', (SPEC / ('method-' + name + '.json')).read_bytes())
            self.validate('investment.stage_definition', method['runtime'])
            self._definition(method['runtime'], stage_id)
            self.stage_methods[stage_id] = method

    def validate(self, schema_id, value):
        result = super().validate(schema_id, value)
        if schema_id == 'investment.funding_plan':
            recorded = instant(value['recorded_at'])
            require(recorded <= instant(now()), 'RS03', '/recorded_at', 'Funding record time cannot be in the future')
            require(not is_known(value['declared_at']) or instant(value['declared_at']['value']) <= recorded,
                    'RS03', '/declared_at', 'Funding declaration time exceeds its actual recording time')
            if is_known(value['future_contributions']):
                ids = [item['contribution_id'] for item in value['future_contributions']['value']]
                require(len(ids) == len(set(ids)), 'RS02', '/future_contributions', 'Duplicate declared funding contribution')
        return result

    def _resource(self, filename, resource_id):
        raw = (SPEC / filename).read_bytes()
        document = json.loads(raw)
        Draft202012Validator.check_schema(document)
        require(document['$id'] == resource_id and resource_id not in self.resources,
                'RS02', '/$id', 'Resource identity conflict')
        self.resources = self.resources.with_resource(resource_id, Resource.from_contents(document))
        self.documents[resource_id] = document
        self.installed_files[resource_id] = filename
        self.installed_hashes[resource_id] = digest(raw)

    def _register(self, registry):
        identifiers = [item['schema_id'] for item in registry['schemas']]
        require(len(identifiers) == len(set(identifiers)), 'RS02', '/schemas', 'Duplicate schema registration')
        for item in registry['schemas']:
            previous = self.targets.get(item['schema_id'])
            require(previous is None or previous == item, 'RS02', '/schemas', 'Conflicting schema registration')
            require(item['schema_version'] == 1 and item['media_type'] in ('application/json', 'text/plain', 'text/markdown'),
                    'RS02', '/schemas', 'Unsupported schema media/version')
            self.resources.resolver().lookup(item['target'])
            self.targets[item['schema_id']] = item

    def _definition(self, definition, stage_id):
        rules = definition['required_rules']
        require(definition['stage_id'] == stage_id and len(rules) == len(set(rules)),
                'RS02', '/runtime', 'Stage identity or duplicate rules')
        for key in ('critical_rules',):
            require(len(definition[key]) == len(set(definition[key])) and set(definition[key]) <= set(rules),
                    'RS02', '/runtime/' + key, 'Unknown or duplicate required rule')
        for key, field_name in (('input_roles', 'role'), ('host_steps', 'step_id'), ('use_rules', 'use')):
            values = [item[field_name] for item in definition[key]]
            require(len(values) == len(set(values)), 'RS02', '/runtime/' + key, 'Duplicate identity')
        for use in definition['use_rules']:
            required = use['required_pass_rules']
            require(len(required) == len(set(required)) and set(required) <= set(rules),
                    'RS02', '/runtime/use_rules', 'Unknown or duplicate use rule')
        name = stage_id.lower()
        require(definition['producer_entry'] == 'process_' + name + '.py'
                and definition['validator_entry'] == 'validate_' + name + '.py',
                'RS02', '/runtime', 'Stage executable identity differs')
        identifiers = [definition['parameters_schema_id'], definition['payload_schema_id']]
        identifiers += [item['schema_id'] for item in definition['input_roles']]
        identifiers += [item['output_schema_id'] for item in definition['host_steps']]
        require(all(sid in self.targets for sid in identifiers), 'RS02', '/runtime', 'Unregistered runtime schema')
        for step in definition['host_steps']:
            path = Path(step['prompt_path'])
            require(path.parts[:2] == ('prompts', name) and '..' not in path.parts,
                    'RS02', '/runtime/host_steps', 'Prompt does not belong to this stage')
        grants = definition['reference_grants']
        require(len({item['schema_id'] for item in grants}) == len(grants)
                and all(item['schema_id'] in self.targets for item in grants),
                'RS02', '/runtime/reference_grants', 'Duplicate or unregistered reference grant schema')

    def _stage_registry(self, stage_id):
        registry = self.stage_registries[stage_id]
        validator = Draft202012Validator({'$ref': 'urn:investment:spec:v1:research#/$defs/StageRegistry'},
                                         registry=self.resources, format_checker=self.format_checker)
        error = next(validator.iter_errors(registry), None)
        require(error is None, 'RS02', '/registry', str(error.message)[:800] if error else '')
        name = stage_id.lower()
        require(registry['enabled_stages'] == [stage_id] and registry['producer_id'] == name + '-producer-v1'
                and registry['validator_id'] == name + '-validator-v1' and registry['rule_set'] == 'investment.' + name + '.rules',
                'RS02', '/registry', 'Registry identities differ from declared stage')
        tables = registry['tables']
        require(len({table['schema_id'] for table in tables}) == len(tables), 'RS02', '/tables', 'Duplicate table registration')
        for table in tables:
            require(table['schema_id'] in self.targets and table['row_schema_id'] in self.targets,
                    'RS02', '/tables', 'Unregistered table or row schema')
            fields = self.row_fields(table['row_schema_id'])
            fixed, dynamic = set(table['units']), set(table.get('row_units', {}))
            require(not fixed & dynamic and fixed | dynamic == fields and set(table['null_policy']) == fields
                    and set(table['primary_key']) <= fields, 'RS02', '/tables', 'Incomplete or conflicting column semantics')
            for column, unit in table.get('row_units', {}).items():
                require(column in fields and set(unit.values()) <= fields,
                        'RS02', '/row_units', 'Dynamic unit or currency field missing from row schema')

    def row_fields(self, schema_id):
        resolved = self.resources.resolver().lookup(self.targets[schema_id]['target'])

        def fields(node, resolver, seen):
            result = set(node.get('properties', {}))
            if '$ref' in node and node['$ref'] not in seen:
                target = resolver.lookup(node['$ref'])
                result |= fields(target.contents, target.resolver, seen | {node['$ref']})
            for keyword in ('allOf', 'oneOf', 'anyOf'):
                for child in node.get(keyword, []):
                    result |= fields(child, resolver, seen)
            return result

        return fields(resolved.contents, resolved.resolver, set())

    def definition(self, stage_id):
        require(stage_id in self.stage_methods, 'RS02', '/stage_id', 'Stage was not explicitly loaded')
        return self.stage_methods[stage_id]['runtime']


@dataclass
class StageContext:
    """本attempt的受限能力；不授予业务有效性或默认通过凭证。"""

    root: Path
    mode: str
    stage_id: str
    run_id: str
    invocation_id: str
    attempt_no: int
    role: str
    schemas: ResearchSchemas
    resolver: object
    input: dict
    input_ref: dict
    method: dict
    method_ref: dict
    definition: dict
    attempt_dir: Path
    candidate: dict | None = None
    candidate_ref: dict | None = None
    feedback: list = field(default_factory=list)
    frozen_host_steps: list = field(default_factory=list)
    upstream_receipts: InitVar[Mapping[str, tuple[dict, dict]] | None] = None

    def __post_init__(self, upstream_receipts):
        self._upstream_receipts = MappingProxyType({
            role: (canonical(reference), canonical(receipt))
            for role, (reference, receipt) in (upstream_receipts or {}).items()
        })
        for reference_raw, _ in self._upstream_receipts.values():
            reference = parse_news(reference_raw)
            self.resolver.allow(reference)
            self.resolver.opaque_refs.add(reference['uri'])

    def read_role(self, role, expected=None):
        rows = [item for item in self.input['input_refs'] if item['role'] == role]
        require(len(rows) == 1, 'RS03', '/input_refs', 'Unknown or ambiguous input role')
        return self.resolver.resolve(rows[0]['ref'], expected)

    def upstream_receipt(self, role):
        """只读该角色真实上游凭证，不授予凭证内候选或依据的读取权限。"""
        from investment.research.references import ResearchResolver
        roles = [item for item in self.definition['input_roles'] if item['role'] == role]
        require(len(roles) == 1 and roles[0]['source']['kind'] == 'upstream_output',
                'RS06', '/role', 'Role is not a registered upstream output', 'use_not_allowed')
        require(role in self._upstream_receipts, 'RS03', '/role', 'No verified receipt was bound for this upstream role', 'missing_evidence')
        reference_raw, receipt_raw = self._upstream_receipts[role]
        reference = parse_news(reference_raw)
        restricted = ResearchResolver(self.root, self.schemas, mode=self.mode, allowed_refs=(reference,), reference_grants=())
        receipt = restricted.resolve(reference, 'investment.validation_receipt')
        source = roles[0]['source']
        require(canonical(receipt) == receipt_raw and receipt['run_id'] == self.run_id and receipt['stage_id'] == source['stage_id']
                and set(source['required_uses']) <= set(receipt['allowed_uses']),
                'RS03', '/upstream_receipt', 'Upstream receipt changed from its verified role binding', 'identity_mismatch')
        return reference, receipt

    def _put(self, filename, raw, schema_id):
        require(re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(json|md|txt)', filename) is not None
                and '..' not in filename, 'RS01', '/filename', 'Invalid artifact filename')
        require(filename not in ('input.json', 'method.json', 'candidate.json', 'receipt.json', 'sealed.json', 'failure.json')
                and not filename.startswith('host-'), 'RS06', '/filename', 'Runtime-owned artifacts cannot be written by stage handlers', 'use_not_allowed')
        require(self.role != 'validator' or filename.startswith('validation-'),
                'RS06', '/filename', 'Validator may only write its own validation artifacts', 'use_not_allowed')
        path = self.attempt_dir / filename
        require(self.attempt_dir.resolve().is_relative_to(self.root / 'runs')
                and path.resolve().parent == self.attempt_dir.resolve(), 'RS01', '/filename', 'Artifact escapes attempt')
        self.schemas.document(schema_id, raw) if self.schemas.targets[schema_id]['media_type'] == 'application/json' else self.schemas.validate(schema_id, raw.decode('utf-8'))
        atomic(path, raw, immutable=True)
        uri = 'investment-run://' + self.stage_id.lower() + '/' + path.relative_to(self.root / 'runs' / self.stage_id.lower()).as_posix()
        reference = artifact(raw, schema_id, uri)
        reference['media_type'] = self.schemas.targets[schema_id]['media_type']
        self.resolver.allow(reference)
        self.resolver.resolve(reference, schema_id)
        return reference

    def put_json(self, filename, value, schema_id):
        require(self.schemas.targets[schema_id]['media_type'] == 'application/json', 'RS02', '/media_type', 'JSON media required')
        return self._put(filename, canonical(value), schema_id)

    def put_text(self, filename, text, schema_id):
        require(self.schemas.targets[schema_id]['media_type'] in ('text/plain', 'text/markdown'), 'RS02', '/media_type', 'Text media required')
        return self._put(filename, text.encode('utf-8'), schema_id)

    def dataset_ref(self, value, schema_id, uri):
        reference = self.resolver.dataset_ref(value, schema_id, uri)
        self.resolver.resolve(reference)
        return reference

    def request_host(self, step_id, allowed_evidence_refs, *, work_key='main'):
        from investment.research.host import create_host_request
        return create_host_request(self, step_id, allowed_evidence_refs, work_key=work_key)

    def host_response(self, step_id, *, work_key='main'):
        from investment.research.host import read_host_response
        return read_host_response(self, step_id, work_key=work_key)
