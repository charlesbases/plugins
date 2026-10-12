# 历史档案来源版本工序

本工序只验证提供者实际保存的发布者版本，`numeric_qualification=false`、`historical_review_qualification=false`。它不接入 revision 1 的行业模型、历史 news-review 或交易资格。历史 deterministic features 需要先明示两类可知集及新 revision，不能修改过去系统 capture/review 时间。

## 固定输入输出（先于实现）

`archives.commoncrawl.collect(root, plan, operation_id, request)` 的 request 必需且仅有：`provider_id, collection_id, original_url, capture_timestamp, timeout_seconds, max_source_operations, max_total_bytes, max_elapsed_seconds`，可选 `claimed_original_receipt_ref`。provider_id 必须为 `commoncrawl`；请求明示精确 collection 与14位 timestamp，但这两个请求值必须由实际官方 index 返回证实。预算为 collector transport invocation 的保守共同预算，失败/中断也收费；重启不重置。没有 caller capture/verified/hash 入口。

collector 先封存 request、实现文件 code_identity 与 `archive-provider-policy.json` 的原字节身份，再按正式白名单 HTTPS 真实取得 inventory、精确 CDX、精确 WARC range。每次实际调用前 SourceJournal reserve，原 metadata/bytes 与 terminal attempt 原子写入，最后封存 operation result。提供者 failures 留 unknown，不能调用其他 host 或未登记路径来替代。旧探测 bytes/receipt 只作明确未认证的控制材料，不复制为本工序网络 journal。

完整 collector result CAS 使用 `archive_source_capture_v1`，必需字段：`schema_id, operation_id, request_ref, protocol_ref, provider_registry_ref, input_inventory_ref, index_ref, range_ref, exact_index_row_ref, publisher_payload_ref, original_http_headers_ref, source_attempts_ref, source_reservations_ref, consumed, status, unknown, claimed_original_receipt_ref, numeric_qualification, historical_review_qualification`。不可得 refs 为 null，并保留原因。输入 inventory/index/range refs 均指向真实 source original 的 `{capture,raw_ref}`；不是 caller 伪装成 fetch 成功的字典。collector 可将原 WARC 解析派生的 payload/header byte refs封存，独立 validator 只读原 bytes再重建比较，不能自己写 CAS补缺引用。

`archives.version.validate(root, plan, capture_ref)` 输入上面确切 collector CAS；独立核本次原 operation request/completion、protocol/current code、SourceJournal original、全部 actual bytes 和 costs、实际 collector 时钟。输出 `provider_archived_publisher_version_v1`：

```text
schema_id, capture_ref, provider_id, protocol_ref, provider_registry_ref, parser_code_identity
qualification = provider_version_verified|unknown
verified_at, index_attested_at, system_retrieved_at
claimed_original_system_retrieved_at, claimed_original_transport_origin=unknown|null
provider_capture_at, provider_capture_precision
provider_public_release_at=null, historical_public_information_bound
original_url, archive_record_id, archive_record_type
publisher_payload_ref, payload_sha256, provider_payload_digest
publisher_response_status, publisher_media_type, original_http_headers_ref
numeric_qualification=false, historical_review_qualification=false
scope, unknown[]
```

`historical_public_information_bound` 固定为 `{status: "unknown", reason: "provider_capture_does_not_prove_archive_public_release_or_investor_knowledge"}`。本工序从不把 provider capture 或 digest 自洽推成当时公开整包、本插件当年已知或可直接进入历史模型的信息集；新 revision 需另定义和核验。provider_public_release_at 始终 null，另保留确切 provider_capture_at 这个已见证的记录时间。

`verified_at` 是验证发生的真实当前时钟；`index_attested_at/system_retrieved_at` 来自当前实际 collector 的两个原 source clocks。旧 receipt 仅能填 `claimed_original_system_retrieved_at`，其 transport origin 保持 unknown；不得用来设定本机当年已读。`provider_capture_at` 来自真实 WARC-Date，并与正式 CDX timestamp 对应；不能从 URL、文件年月、网页发布日期或 caller 值创造。来源版本状态不意味着新闻事实、因果、整个网站、历史人工经济评审或模型资格。

`parser_code_identity` 为原 operation 事前封存的 `verify.code_identity()`，独立验证时与当前实际身份相等；它包含 scripts 下 archives 模块、scripts JSON及requirements，不包含references policy。policy 另由 protocol_ref 的实际原字节与当前 policy 字节精确核验，collector完成前再核实际原字节未变。`provider_registry_ref` 是该已核原 protocol 内 source_registry 的 CAS，并精确比较实际值；不是 caller 提供的通用注册表。

## 首个精确提供者协议

唯一网络 inventory 是 `https://index.commoncrawl.org/collinfo.json`。collection 的 cdx-api 必须来自该实际原 JSON，且为已登记 index host 的相同 collection endpoint。index 查询只有原 URL、JSON output 和 status=200 filter。实际 CDX row 需要精确 original URL/所求 timestamp、status200、native filename/offset/length/digest；不使用“closest”或当前 live 网页替代。

range 仅为 `https://data.commoncrawl.org/` 下该 native filename，Range 恰好对应原 offset/length。核实际206、完整 Content-Range/bytes、gzip、有限解压、WARC Content-Length、record type/id/date/target、原 HTTP block/payload、原 CDX与WARC payload SHA1及WARC block SHA1，并另封存本机 SHA256。返回 checksum必须以真正取得的官方 index 为依据，不能由 caller 同时提供匹配 digest 和 payload。index/crawl self-consistency 加实际 provider transport lineage才认证提供者版本。[CC 官方格式](https://commoncrawl.org/cdxj-index)

Common Crawl 是 web sample，不能据它声称整个网站/行业/世界新闻完整；全查询/失败/未取得域保持 unknown。collection metadata 仅证明已声明的 inventory，不表示本机过去已读取、提供者当时已公开发布整包或投资者当时知悉。旧 source 可重放原 bytes作控制对照，正式采集时钟仍为本次真实时钟。[CC 官方FAQ](https://commoncrawl.org/faq)

IA 不在首个实现中以未校验方式获得资格；未来另登记 CDX/TimeMap/actual replay、original/Memento clock/digest/revisit完整链。availability closest或Offline HTML不等同原文；未支持 provider 必须返回/拒绝为unknown，不映射为CC。不以未经定义的通用 archive URL importer进入旧模型。[IA 官方接口](https://archive.org/help/wayback_api.php)

首个真实工程正例可使用已探测的 Fed record 做小型 actual HTTPS inventory+CDX+12,899-byte WARC range acquisition，保留旧2026receipt作为未认证对照，独立验证 provider2024/current取得/current验证三类时钟；该工程正例不是主链或市场验收。controlled negatives只标 auxiliary。正式历史模型仍须新revision、足够成熟原点、独立校准及全部金融来源资格，原799/q/CVaR不降低。
