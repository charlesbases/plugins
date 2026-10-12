"""可插拔采集的统一异步协议；捕获和文件写入由受信上下文负责。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Generic, Literal, Protocol, TypeVar, TypedDict

T = TypeVar('T')
MaterialKind = Literal['article', 'official_release', 'regulatory_filing', 'event_record']
SourceStatus = Literal['completed', 'partial', 'failed', 'unsupported']
CaptureState = Literal['complete', 'partial', 'failed']
FetchPurpose = Literal['entry', 'listing', 'document', 'data']
Disposition = Literal['captured', 'excluded_outside_window', 'excluded_kind', 'duplicate_version',
                      'pending_fetch', 'pending_parse', 'failed_fetch']
Use = Literal['evidence_review', 'hypothesis_research', 'reporting']


class Known(TypedDict, Generic[T]):
    status: Literal['known']
    value: T
    reason: Literal['']


class Unknown(TypedDict):
    status: Literal['unknown', 'not_applicable']
    value: None
    reason: str


OptionalValue = Known[T] | Unknown


class ArtifactRef(TypedDict):
    artifact_id: str
    schema_id: str
    schema_version: Literal[1]
    media_type: Literal['application/json']
    uri: str
    sha256: str
    byte_size: int


class TimeBounds(TypedDict):
    lower_bound: str
    upper_bound: str
    precision: Literal['instant', 'date', 'month', 'quarter', 'year', 'interval']
    timezone: str
    basis_ref: ArtifactRef


class NewsWindow(TypedDict):
    window_id: str
    start_at: str
    end_at: str


class Limits(TypedDict):
    global_concurrency: int
    per_source_concurrency: int
    timeout_ms: int
    response_byte_limit: int
    decoded_byte_limit: int
    request_budget: int
    page_budget: int
    document_budget: int
    redirect_budget: int


class CollectorRequest(TypedDict):
    request_id: str
    source_id: str
    entrypoints: list[str]
    window: NewsWindow
    information_cutoff_at: str
    limits: Limits


class Header(TypedDict):
    name: str
    value: str


class CaptureError(TypedDict):
    code: Literal['timeout', 'network', 'http_status', 'redirect_limit', 'response_byte_limit', 'cancelled', 'decode_error']
    message: str


class Capture(TypedDict):
    capture_id: str
    request_id: str
    source_id: str
    sequence_no: int
    parent_capture_id: OptionalValue[str]
    purpose: FetchPurpose
    method: Literal['GET']
    requested_url: str
    final_url: str
    request_headers: list[Header]
    response_headers: list[Header]
    http_status: OptionalValue[int]
    redirect_target: OptionalValue[str]
    request_started_at: str
    response_finished_at: str
    capture_state: CaptureState
    origin: Literal['live', 'synthetic']
    response_bytes_base64: str
    response_sha256: str
    response_byte_size: int
    error: OptionalValue[CaptureError]


class BodyArtifact(TypedDict):
    source_id: str
    capture_ref: ArtifactRef
    profile_ref: ArtifactRef
    format: Literal['html_text', 'xml_text', 'json_record']
    title: str
    text: str
    source_time: OptionalValue[str]
    title_locator: str
    body_locator: str
    time_locator: str
    native_media_type: Literal['text/html', 'application/xml', 'application/json', 'application/geo+json']


class DocumentRow(TypedDict):
    document_id: str
    version_id: str
    publisher_id: str
    source_id: str
    title: str
    material_kind: MaterialKind
    requested_url: str
    final_url: str
    raw_ref: ArtifactRef
    body_ref: ArtifactRef
    published_time: OptionalValue[TimeBounds]
    window_time: OptionalValue[TimeBounds]
    window_time_role: Literal['published', 'event_occurrence', 'filing_accepted', 'unknown']
    window_membership: Literal['within', 'uncertain']
    available_time: OptionalValue[TimeBounds]
    retrieved_at: str
    availability_basis: Literal['observed_response', 'unknown_before_cutoff']
    capture_status: Literal['complete']


class DiscoveryLocator(TypedDict):
    capture_ref: ArtifactRef
    locator: str


class OpportunityRow(TypedDict):
    opportunity_id: str
    source_id: str
    url: str
    material_kind: MaterialKind
    discovered_in: list[DiscoveryLocator]
    listed_title: OptionalValue[str]
    listed_time: OptionalValue[str]
    disposition: Disposition
    document_ids: list[str]
    reason: str


class PendingRow(TypedDict):
    pending_id: str
    source_id: str
    opportunity_id: str
    reason_code: Literal['pending_fetch', 'pending_parse', 'failed_fetch']
    required_evidence: Literal['source_response', 'supported_layout']
    affected_uses: list[Use]


class SourceError(TypedDict):
    source_id: str
    code: Literal['unsupported_source', 'unresolved_profile', 'request_failed', 'layout_unknown', 'budget_stop', 'coverage_unknown']
    capture_id: OptionalValue[str]
    message: str


class CoverageRow(TypedDict):
    source_id: str
    status: SourceStatus
    listing_state: Literal['exhausted_exposed', 'verified_window_boundary', 'budget_stop', 'error', 'unknown']
    window_coverage: Literal['complete', 'unknown']
    configured_entrypoints: list[str]
    visited_entrypoints: list[str]
    capture_ids: list[str]
    opportunity_ids: list[str]
    document_ids: list[str]
    pending_ids: list[str]
    errors: list[SourceError]
    reason: str


class CollectorResult(TypedDict):
    request_id: str
    source_id: str
    status: SourceStatus
    documents: list[DocumentRow]
    opportunities: list[OpportunityRow]
    pending_items: list[PendingRow]
    coverage: CoverageRow
    errors: list[SourceError]


@dataclass(frozen=True)
class CapturedResponse:
    """一次实际 HTTP hop；encoded_bytes 为移除传输帧但未解 Content-Encoding 的实体。"""
    capture_ref: ArtifactRef
    envelope: Capture
    encoded_bytes: bytes


@dataclass(frozen=True)
class FrozenSourceConfig:
    """本调用已冻结规则的只读视图；引用每次解码为副本，bytes不可变。"""
    source_id: str
    _manifest_ref_json: bytes
    _profile_ref_json: bytes
    manifest_json: bytes
    profile_json: bytes

    @property
    def manifest_ref(self) -> ArtifactRef:
        return json.loads(self._manifest_ref_json)

    @property
    def profile_ref(self) -> ArtifactRef:
        return json.loads(self._profile_ref_json)


class CollectorContext(Protocol):
    """上下文绑定请求身份/全局资源预算，每次网络调用先登记后封存，禁止插件自制历史。"""

    @property
    def frozen_source(self) -> FrozenSourceConfig:
        """由NewsResolver.source_config按当前request.source_id构造，返回引用副本。"""
        ...

    async def fetch(self, url: str, *, purpose: FetchPurpose,
                    parent_capture_id: str | None = None) -> CapturedResponse:
        """每个重定向 hop 单独调用；返回真实失败也必须有 capture_ref。"""
        ...

    async def emit_body(self, body: BodyArtifact) -> ArtifactRef:
        """核对本上下文捕获及冻结 profile，规范编码并保存正文，不授予下游许可。"""
        ...


class Collector(Protocol):
    async def __call__(self, request: CollectorRequest, context: CollectorContext) -> CollectorResult:
        ...
