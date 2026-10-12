# 公共及 SC01 Schema 派生字段索引

本页由 common、sc01、storage、runs Schema 展开，供字段查询；精确 oneOf、required、pattern 及额外字段政策以对应 Schema 为准，跨对象谓词见 [SC01 规范](sc01.md)。本页不定义第二套约束。SC02 全字段直接查询 [sc02.schema.json](sc02.schema.json)，语义关系见 [SC02 规范](sc02.md)。业务字段的必要性与消费者见 [SC01 外部说明](../../chains/account/README.md#字段必要性与消费者)。

## common.schema.json

### ID

`{"type":"string","minLength":1,"maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"}`

### PathID

`{"type":"string","minLength":1,"maxLength":64,"pattern":"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"}`

### SchemaID

`{"type":"string","pattern":"^investment\\.[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$","maxLength":128}`

### Instant

`{"type":"string","format":"date-time","pattern":"^[0-9]{4}-(0[1-9]&#124;1[0-2])-(0[1-9]&#124;[12][0-9]&#124;3[01])T([01][0-9]&#124;2[0-3]):[0-5][0-9]:[0-5][0-9](\\.[0-9]{1,6})?(Z&#124;[+-](0[0-9]&#124;1[0-3]):[0-5][0-9]&#124;[+-]14:00)$"}`

### UTCInstant

`{"type":"string","format":"date-time","pattern":"^[0-9]{4}-(0[1-9]&#124;1[0-2])-(0[1-9]&#124;[12][0-9]&#124;3[01])T([01][0-9]&#124;2[0-3]):[0-5][0-9]:[0-5][0-9](\\.[0-9]{1,6})?Z$"}`

### Date

`{"type":"string","format":"date","pattern":"^[0-9]{4}-(0[1-9]&#124;1[0-2])-(0[1-9]&#124;[12][0-9]&#124;3[01])$"}`

### ExecutionDate

`{"type":"string","pattern":"^[0-9]{8}$"}`

### DecimalString

`{"type":"string","pattern":"^-?(0&#124;[1-9][0-9]{0,29})(\\.[0-9]{1,18})?$"}`

### NonnegativeDecimal

`{"type":"string","pattern":"^(0&#124;[1-9][0-9]{0,29})(\\.[0-9]{1,18})?$"}`

### Hash

`{"type":"string","pattern":"^[0-9a-f]{64}$"}`

### Count

`{"type":"integer","minimum":0,"maximum":9007199254740991}`

### Use

`{"enum":["evidence_review","hypothesis_research","current_account_review","pit_training","selection_comparison","independent_evaluation","decision_advice","reporting"]}`

### StageID

`{"enum":["SC01","SC02","SC03","SC04","SC05","SC06","SC07","SC08","SC09","SC10"]}`

### ProcessingState

`{"enum":["pending","running","completed","failed"]}`

### CheckVerdict

`{"enum":["passed","failed","insufficient_evidence","not_evaluated","not_applicable"]}`

### OverallVerdict

`{"enum":["passed","partial","failed"]}`

### BusinessResult

`{"enum":["usable","observation_only","blocked","rejected"]}`

### MediaType

`{"enum":["application/json","text/plain","text/markdown"]}`

### URI

`{"type":"string","format":"uri","maxLength":4096,"pattern":"^(investment-data&#124;investment-run&#124;investment-spec&#124;fixture)://"}`

### Unit

`{"enum":["CNY","share","ratio","day","count","identifier","time","code","reference","unitless"]}`

### ArtifactRef

| 字段 | 必填 | 格式约束 |
|---|---|---|
| artifact_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| schema_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/SchemaID"}` |
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| media_type | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/MediaType"}` |
| uri | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/URI"}` |
| sha256 | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"}` |
| byte_size | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |

### ColumnUnits

`{"type":"object","minProperties":1,"propertyNames":{"type":"string","pattern":"^[a-z][a-z0-9_]*$"},"additionalProperties":{"$ref":"urn:investment:spec:v1:common#/$defs/Unit"}}`

### ColumnNullPolicy

`{"type":"object","minProperties":1,"propertyNames":{"type":"string","pattern":"^[a-z][a-z0-9_]*$"},"additionalProperties":{"enum":["forbidden","optional_value"]}}`

### DatasetRef

| 字段 | 必填 | 格式约束 |
|---|---|---|
| artifact_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| schema_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/SchemaID"}` |
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| media_type | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/MediaType"}` |
| uri | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/URI"}` |
| sha256 | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"}` |
| byte_size | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |
| table_schema_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| row_count | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |
| primary_key | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ID"},"minItems":1,"uniqueItems":true}` |
| units | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ColumnUnits"}` |
| null_policy | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ColumnNullPolicy"}` |

### Reference

`{"oneOf":[{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},{"$ref":"urn:investment:spec:v1:common#/$defs/DatasetRef"}]}`

### OptionalDecimal

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/DecimalString"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalAmount

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/NonnegativeDecimal"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalShare

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/NonnegativeDecimal"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalID

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/ID"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalInstant

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalArtifact

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalPositiveInt

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"integer","minimum":1,"maximum":9007199254740991},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalBoolean

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"boolean"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalFraction

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/NonnegativeDecimal"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### RawTime

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["kind","value"],"properties":{"kind":{"const":"instant"},"value":{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}}},{"type":"object","additionalProperties":false,"required":["kind","value","timezone"],"properties":{"kind":{"const":"date"},"value":{"$ref":"urn:investment:spec:v1:common#/$defs/Date"},"timezone":{"enum":["UTC","Asia/Shanghai"]}}}]}`

### OptionalRawTime

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/RawTime"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### TimeEvidenceBody

| 字段 | 必填 | 格式约束 |
|---|---|---|
| lower_bound | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| upper_bound | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| precision | 是 | `{"enum":["instant","date","month","quarter","year","interval"]}` |
| timezone | 是 | `{"enum":["UTC","Asia/Shanghai"]}` |
| basis_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |

### TimeEvidence

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/TimeEvidenceBody"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### Gap

| 字段 | 必填 | 格式约束 |
|---|---|---|
| rule_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| field_or_object | 是 | `{"type":"string","pattern":"^(/([^~/]&#124;~[01])*)+$","maxLength":1024}` |
| reason | 是 | `{"type":"string","minLength":1,"maxLength":2048}` |
| owner | 是 | `{"enum":["user","controller","producer","validator","storage","source"]}` |
| required_evidence | 是 | `{"type":"array","items":{"enum":["account_confirmation","cash_confirmation","holdings_confirmation","orders_confirmation","transit_confirmation","liability_confirmation","principal_baseline","cashflow_history","mandate_confirmation","source_bytes","parent_revision","storage_reconciliation","protocol_definition","transaction_confirmation","share_confirmation","settlement_confirmation"]},"minItems":1,"uniqueItems":true}` |
| affected_uses | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Use"},"minItems":1,"uniqueItems":true}` |

### InputRef

| 字段 | 必填 | 格式约束 |
|---|---|---|
| role | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Reference"}` |

### StageInput

| 字段 | 必填 | 格式约束 |
|---|---|---|
| contract_id | 是 | `{"const":"investment.stage_input"}` |
| contract_version | 是 | `{"const":1,"type":"integer"}` |
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| decision_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| information_cutoff_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| scope_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| input_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/InputRef"},"minItems":1}` |
| parameters_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |

### CandidateOutput

| 字段 | 必填 | 格式约束 |
|---|---|---|
| contract_id | 是 | `{"const":"investment.candidate_output"}` |
| contract_version | 是 | `{"const":1,"type":"integer"}` |
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| output_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| created_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| processing_state | 是 | `{"const":"completed"}` |
| input_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/InputRef"},"minItems":1}` |
| scope_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| method_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| payload_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| datasets | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/DatasetRef"},"minItems":0}` |
| evidence_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"minItems":0}` |
| gaps | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Gap"},"minItems":0}` |

### Check

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["rule_id","verdict","reason","evidence_refs"],"properties":{"rule_id":{"$ref":"urn:investment:spec:v1:common#/$defs/ID"},"verdict":{"$ref":"urn:investment:spec:v1:common#/$defs/CheckVerdict"},"reason":{"type":"string","minLength":0,"maxLength":2048},"evidence_refs":{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"minItems":0}}},{"type":"object","additionalProperties":false,"required":["rule_id","verdict","reason","evidence_refs","actual","threshold","unit","tolerance"],"properties":{"rule_id":{"$ref":"urn:investment:spec:v1:common#/$defs/ID"},"verdict":{"$ref":"urn:investment:spec:v1:common#/$defs/CheckVerdict"},"reason":{"type":"string","minLength":0,"maxLength":2048},"evidence_refs":{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"minItems":0},"actual":{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalDecimal"},"threshold":{"$ref":"urn:investment:spec:v1:common#/$defs/DecimalString"},"unit":{"$ref":"urn:investment:spec:v1:common#/$defs/Unit"},"tolerance":{"$ref":"urn:investment:spec:v1:common#/$defs/NonnegativeDecimal"}}}]}`

### ValidationReceipt

| 字段 | 必填 | 格式约束 |
|---|---|---|
| validator_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| validator_version | 是 | `{"const":"v1"}` |
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| input_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/InputRef"},"minItems":1}` |
| candidate_output_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| validated_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| checks | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Check"},"minItems":1}` |
| overall_verdict | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OverallVerdict"}` |
| business_result | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/BusinessResult"}` |
| allowed_uses | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Use"},"minItems":0,"uniqueItems":true}` |
| blockers | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Gap"},"minItems":0}` |

### SealedOutput

| 字段 | 必填 | 格式约束 |
|---|---|---|
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| output_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| candidate_output_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| validation_receipt_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| sealed_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |

### ExecutionRecord

| 字段 | 必填 | 格式约束 |
|---|---|---|
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| input_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| processing_state | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ProcessingState"}` |
| started_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalInstant"}` |
| finished_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalInstant"}` |
| failure_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |

### ParameterSpec

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["unit","source_kind","basis_ref","determination_rule","determined_at","name","value"],"properties":{"unit":{"$ref":"urn:investment:spec:v1:common#/$defs/Unit"},"source_kind":{"enum":["engineering_budget","registered_business_rule"]},"basis_ref":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"determination_rule":{"const":"constant_v1"},"determined_at":{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"},"name":{"const":"max_attempts"},"value":{"const":3,"type":"integer"}}},{"type":"object","additionalProperties":false,"required":["unit","source_kind","basis_ref","determination_rule","determined_at","name","value"],"properties":{"unit":{"$ref":"urn:investment:spec:v1:common#/$defs/Unit"},"source_kind":{"enum":["engineering_budget","registered_business_rule"]},"basis_ref":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"determination_rule":{"const":"constant_v1"},"determined_at":{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"},"name":{"const":"max_json_bytes"},"value":{"const":10485760,"type":"integer"}}},{"type":"object","additionalProperties":false,"required":["unit","source_kind","basis_ref","determination_rule","determined_at","name","value"],"properties":{"unit":{"$ref":"urn:investment:spec:v1:common#/$defs/Unit"},"source_kind":{"enum":["engineering_budget","registered_business_rule"]},"basis_ref":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"determination_rule":{"const":"constant_v1"},"determined_at":{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"},"name":{"const":"decimal_precision"},"value":{"const":80,"type":"integer"}}},{"type":"object","additionalProperties":false,"required":["unit","source_kind","basis_ref","determination_rule","determined_at","name","value"],"properties":{"unit":{"$ref":"urn:investment:spec:v1:common#/$defs/Unit"},"source_kind":{"enum":["engineering_budget","registered_business_rule"]},"basis_ref":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"determination_rule":{"const":"constant_v1"},"determined_at":{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"},"name":{"const":"execution_timezone"},"value":{"const":"Asia/Shanghai"}}}]}`

### EstimateAmount

| 字段 | 必填 | 格式约束 |
|---|---|---|
| value | 是 | `{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/NonnegativeDecimal"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}` |
| value_basis | 是 | `{"const":"estimated"}` |
| assumptions_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| as_of | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| evidence_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"minItems":1}` |

### OptionalNonnegative

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/NonnegativeDecimal"},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### SchemaEntry

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/SchemaID"}` |
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| target | 是 | `{"type":"string","pattern":"^urn:investment:spec:v1:(common&#124;sc01&#124;storage&#124;runs)#/\\$defs/[A-Za-z]+$"}` |
| media_type | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/MediaType"}` |

### TableEntry

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/SchemaID"}` |
| row_schema_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/SchemaID"}` |
| primary_key | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ID"},"minItems":1,"uniqueItems":true}` |
| units | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ColumnUnits"}` |
| null_policy | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ColumnNullPolicy"}` |

### Registry

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| schemas | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/SchemaEntry"}}` |
| tables | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/TableEntry"}}` |
| enabled_stages | 是 | `{"const":["SC01"]}` |
| producer_id | 是 | `{"const":"sc01-producer-v1"}` |
| validator_id | 是 | `{"const":"sc01-validator-v1"}` |
| rule_set | 是 | `{"const":"investment.sc01.rules"}` |

## sc01.schema.json

### OptionalHash

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"},"status":{"const":"known"},"reason":{"type":"string","maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalText

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"string","minLength":1,"maxLength":2048},"status":{"const":"known"},"reason":{"type":"string","maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### Opening

| 字段 | 必填 | 格式约束 |
|---|---|---|
| start_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| positions | 是 | `{"const":"confirmed_empty"}` |
| coverage | 是 | `{"const":"recorded_transactions_since_start"}` |

### Confirmation

| 字段 | 必填 | 格式约束 |
|---|---|---|
| state | 是 | `{"enum":["pending","confirmed","cancelled","failed"]}` |
| actual_at | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalTime"}` |
| basis | 是 | `{"enum":["user_reported","expected_due","not_applicable"]}` |

### Settlement

| 字段 | 必填 | 格式约束 |
|---|---|---|
| state | 是 | `{"enum":["not_applicable","pending","settled"]}` |
| actual_at | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalTime"}` |
| basis | 是 | `{"enum":["user_reported","expected_due","not_applicable"]}` |

### Trade

| 字段 | 必填 | 格式约束 |
|---|---|---|
| trade_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| instrument_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| side | 是 | `{"enum":["buy","sell"]}` |
| amount | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalAmount"}` |
| confirmed_shares | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalShare"}` |
| requested_shares | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalShare"}` |
| fee_amount | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalAmount"}` |
| trade_at | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalTime"}` |
| holding_start | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalTime"}` |
| expected_at | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalTime"}` |
| confirmation | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Confirmation"}` |
| settlement | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Settlement"}` |
| last_arrival_reply | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalArrivalReply"}` |

### Mutation

| 字段 | 必填 | 格式约束 |
|---|---|---|
| change_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| mode | 是 | `{"enum":["create","update","correct"]}` |
| expected_trade_hash | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalHash"}` |
| correction_reason | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalText"}` |
| trade | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/TradeInput"}` |

### Changes

| 字段 | 必填 | 格式约束 |
|---|---|---|
| rows | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:sc01#/$defs/Mutation"},"minItems":0,"maxItems":100000}` |

### SourceRecord

| 字段 | 必填 | 格式约束 |
|---|---|---|
| record_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| source_type | 是 | `{"enum":["user_confirmation","synthetic_fixture"]}` |
| operation | 是 | `{"enum":["initialize","refresh"]}` |
| account_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| confirmed_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| transaction_as_of | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| platform | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| currency | 是 | `{"const":"CNY"}` |
| opening | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Opening"}` |
| changes | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Changes"}` |
| arrival_replies | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/ArrivalReplies"}` |
| base_generation | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |

### TransactionFacts

| 字段 | 必填 | 格式约束 |
|---|---|---|
| source_record_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| base_generation | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |

### TaskScope

| 字段 | 必填 | 格式约束 |
|---|---|---|
| scope_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| account_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| operation | 是 | `{"enum":["initialize","refresh"]}` |
| account_information_cutoff_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| purpose | 是 | `{"enum":["account_review","fixture_validation"]}` |
| requested_uses | 是 | `{"type":"array","items":{"enum":["current_account_review","reporting"]},"minItems":1,"maxItems":2,"uniqueItems":true}` |
| transaction_as_of_target | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |

### Position

| 字段 | 必填 | 格式约束 |
|---|---|---|
| instrument_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| confirmed_shares | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalShare"}` |
| trade_ids | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ID"},"minItems":1,"maxItems":100000}` |
| coverage | 是 | `{"const":"recorded_transactions_since_start"}` |

### Pending

| 字段 | 必填 | 格式约束 |
|---|---|---|
| trade_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| instrument_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| side | 是 | `{"enum":["buy","sell"]}` |
| pending_phase | 是 | `{"enum":["confirmation","settlement"]}` |
| expected_at | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalTime"}` |

### Trades

| 字段 | 必填 | 格式约束 |
|---|---|---|
| rows | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:sc01#/$defs/Trade"},"minItems":0,"maxItems":100000}` |

### Positions

| 字段 | 必填 | 格式约束 |
|---|---|---|
| rows | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:sc01#/$defs/Position"},"minItems":0,"maxItems":100000}` |

### PendingTransactions

| 字段 | 必填 | 格式约束 |
|---|---|---|
| rows | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:sc01#/$defs/Pending"},"minItems":0,"maxItems":100000}` |

### BusinessObjects

| 字段 | 必填 | 格式约束 |
|---|---|---|
| trades | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Trades"}` |
| positions | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Positions"}` |
| pending_transactions | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/PendingTransactions"}` |

### TransactionContext

| 字段 | 必填 | 格式约束 |
|---|---|---|
| account_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| generation | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |
| ledger_digest | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"}` |
| transaction_as_of | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| known_as_of | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| execution_cutoff_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| platform | 是 | `{"type":"string","minLength":1,"maxLength":128}` |
| currency | 是 | `{"const":"CNY"}` |
| opening | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Opening"}` |
| source_authority | 是 | `{"enum":["user_confirmation","synthetic_fixture"]}` |
| trade_count | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |
| trades_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/DatasetRef"}` |
| positions_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/DatasetRef"}` |
| pending_transactions_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/DatasetRef"}` |
| missing_facts | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Gap"}}` |

### ParameterSet

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| stage_id | 是 | `{"const":"SC01"}` |
| parameters | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ParameterSpec"},"minItems":4,"maxItems":4,"uniqueItems":true}` |

### Method

| 字段 | 必填 | 格式约束 |
|---|---|---|
| method_id | 是 | `{"const":"sc01-confirmed-trades"}` |
| method_version | 是 | `{"const":"v1"}` |
| producer_id | 是 | `{"const":"sc01-producer-v1"}` |
| validator_id | 是 | `{"const":"sc01-validator-v1"}` |
| rule_set | 是 | `{"const":"investment.sc01.rules"}` |
| operation_modes | 是 | `{"const":["initialize","refresh"]}` |
| arithmetic | 是 | `{"const":"exact_decimal"}` |
| unknown_policy | 是 | `{"const":"preserve_and_restrict_uses"}` |
| arrival_policy | 是 | `{"const":"explicit_reply_then_expected_due"}` |
| record_unit | 是 | `{"const":"whole_trade"}` |

### OptionalTime

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/RawTime"},"status":{"const":"known"},"reason":{"type":"string","maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### OptionalMoney

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:common#/$defs/NonnegativeDecimal"},"status":{"const":"known"},"reason":{"type":"string","maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### ExplicitArrivalReply

| 字段 | 必填 | 格式约束 |
|---|---|---|
| result | 是 | `{"enum":["completed","not_completed"]}` |
| reported_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |

### OptionalArrivalReply

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"$ref":"urn:investment:spec:v1:sc01#/$defs/ExplicitArrivalReply"},"status":{"const":"known"},"reason":{"type":"string","maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### ArrivalReply

| 字段 | 必填 | 格式约束 |
|---|---|---|
| trade_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| result | 是 | `{"enum":["completed","not_completed","no_reply"]}` |
| reported_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalInstant"}` |
| new_expected_at | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalTime"}` |

### ArrivalReplies

| 字段 | 必填 | 格式约束 |
|---|---|---|
| rows | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:sc01#/$defs/ArrivalReply"},"maxItems":100000}` |

### TradeInput

`{"$ref":"urn:investment:spec:v1:sc01#/$defs/Trade"}`

### ArrivalQuestion

| 字段 | 必填 | 格式约束 |
|---|---|---|
| trade_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| instrument_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| kind | 是 | `{"enum":["buy_confirmation","sell_settlement"]}` |
| expected_at | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/OptionalTime"}` |

### ArrivalPreparation

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"const":1}` |
| stage_id | 是 | `{"const":"SC01"}` |
| account_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| prepared_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| questions | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:sc01#/$defs/ArrivalQuestion"},"maxItems":100000}` |
| base_generation | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |

## storage.schema.json

### CommitStamp

| 字段 | 必填 | 格式约束 |
|---|---|---|
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| execution_date | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ExecutionDate"}` |
| attempt_no | 是 | `{"type":"integer","minimum":1,"maximum":3}` |
| input_sha256 | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"}` |
| candidate_sha256 | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"}` |
| receipt_sha256 | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"}` |
| sealed_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/UTCInstant"}` |
| finalized | 是 | `{"type":"boolean"}` |

### LedgerMeta

| 字段 | 必填 | 格式约束 |
|---|---|---|
| account_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| platform | 是 | `{"type":"string","minLength":1,"maxLength":128}` |
| currency | 是 | `{"const":"CNY"}` |
| opening | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Opening"}` |
| source_authority | 是 | `{"enum":["user_confirmation","synthetic_fixture"]}` |
| generation | 是 | `{"type":"integer","minimum":1,"maximum":9007199254740991}` |
| ledger_digest | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"}` |
| known_as_of | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| transaction_as_of | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| last_commit | 是 | `{"$ref":"urn:investment:spec:v1:storage#/$defs/CommitStamp"}` |

### TradeRecord

| 字段 | 必填 | 格式约束 |
|---|---|---|
| account_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| trade_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| trade_json | 是 | `{"type":"string","minLength":1,"maxLength":10485760}` |

### LedgerBaseline

| 字段 | 必填 | 格式约束 |
|---|---|---|
| meta | 是 | `{"oneOf":[{"type":"null"},{"$ref":"urn:investment:spec:v1:storage#/$defs/LedgerMeta"}]}` |
| trades | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/Trades"}` |

### StagedTransactions

| 字段 | 必填 | 格式约束 |
|---|---|---|
| context | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/TransactionContext"}` |
| source_record | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/SourceRecord"}` |
| business_objects | 是 | `{"$ref":"urn:investment:spec:v1:sc01#/$defs/BusinessObjects"}` |

## runs.schema.json

### Phase

`{"enum":["input_check","production","output_check","commit","finished"]}`

### FailureCategory

`{"enum":["input_schema","input_reference","input_semantic","production","output_schema","output_reference","output_semantic","independent_validation","cas_conflict","storage","runtime_io"]}`

### ReasonCode

`{"enum":["invalid_json","schema_violation","unregistered_schema","invalid_reference","hash_mismatch","identity_mismatch","date_order","source_mismatch","duplicate_key","business_invariant","missing_evidence","use_not_allowed","producer_exception","validator_exception","parent_changed","write_failed","io_failed","checks_incomplete"]}`

### OptionalPointer

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"string","pattern":"^(?:/(?:[^~/]&#124;~[01])*)*$","maxLength":4096},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### InputCheck

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| check_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| validator_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| validator_version | 是 | `{"const":"v1"}` |
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| attempt_no | 是 | `{"type":"integer","minimum":1,"maximum":3}` |
| validated_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| input_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Reference"},"minItems":1,"maxItems":100000}` |
| checks | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Check"},"minItems":1,"maxItems":100000}` |
| overall_verdict | 是 | `{"enum":["passed","failed"]}` |

### Failure

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| failure_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| attempt_no | 是 | `{"type":"integer","minimum":1,"maximum":3}` |
| phase | 是 | `{"enum":["input_check","production","output_check","commit"]}` |
| detected_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| category | 是 | `{"$ref":"urn:investment:spec:v1:runs#/$defs/FailureCategory"}` |
| rule_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| reason_code | 是 | `{"$ref":"urn:investment:spec:v1:runs#/$defs/ReasonCode"}` |
| object_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| json_pointer | 是 | `{"$ref":"urn:investment:spec:v1:runs#/$defs/OptionalPointer"}` |
| reason | 是 | `{"type":"string","minLength":1,"maxLength":2048}` |
| evidence_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"minItems":0,"maxItems":100000}` |
| affected_uses | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Use"},"minItems":1,"maxItems":8,"uniqueItems":true}` |
| owner | 是 | `{"enum":["controller","producer","validator","storage","user"]}` |
| gaps | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/Gap"},"minItems":0,"maxItems":100000}` |

### FailureSummary

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| status | 是 | `{"const":"failed"}` |
| attempt_count | 是 | `{"const":3,"type":"integer"}` |
| failure_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"minItems":3,"maxItems":3}` |
| terminated_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| next_chain_allowed | 是 | `{"const":false}` |
| termination_code | 是 | `{"const":"attempts_exhausted"}` |

### FileRole

`{"enum":["frozen_input","source_record","parameters","method","scope","input_check","candidate","validation","sealed_output","failure","execution","staged_object","terminal_failure","log"]}`

### FileEntry

| 字段 | 必填 | 格式约束 |
|---|---|---|
| role | 是 | `{"$ref":"urn:investment:spec:v1:runs#/$defs/FileRole"}` |
| relative_path | 是 | `{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z0-9_-]+/(?:attempt-0[123]/)?[A-Za-z0-9_.-]+$"}` |
| media_type | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/MediaType"}` |
| sha256 | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Hash"}` |
| byte_size | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |
| purpose_code | 是 | `{"enum":["frozen_context","input_gate","output_gate","business_handoff","failure_explanation","temporary_computation"]}` |

### Retention

| 字段 | 必填 | 格式约束 |
|---|---|---|
| state | 是 | `{"enum":["retain","eligible","cleaned"]}` |
| blockers | 是 | `{"type":"array","items":{"enum":["attempt_unfinished","validation_unfinished","commit_unfinished","active_downstream_use","pending_user_fact","pending_order_reconciliation","required_reference_not_persisted"]},"minItems":0,"maxItems":7,"uniqueItems":true}` |
| persistent_refs | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"},"minItems":0,"maxItems":100000}` |
| evaluated_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalInstant"}` |

### Attempt

| 字段 | 必填 | 格式约束 |
|---|---|---|
| attempt_no | 是 | `{"type":"integer","minimum":1,"maximum":3}` |
| directory | 是 | `{"enum":["attempt-01","attempt-02","attempt-03"]}` |
| state | 是 | `{"enum":["registered","running","passed","failed"]}` |
| phase | 是 | `{"$ref":"urn:investment:spec:v1:runs#/$defs/Phase"}` |
| started_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalInstant"}` |
| finished_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalInstant"}` |
| input_check_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| candidate_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| output_validation_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| sealed_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| execution_record_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| failure_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| files | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:runs#/$defs/FileEntry"},"minItems":0,"maxItems":100000}` |

### OptionalAttempt

`{"oneOf":[{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"integer","minimum":1,"maximum":3},"status":{"const":"known"},"reason":{"type":"string","minLength":0,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"unknown"},"reason":{"type":"string","minLength":1,"maxLength":2048}}},{"type":"object","additionalProperties":false,"required":["value","status","reason"],"properties":{"value":{"type":"null"},"status":{"const":"not_applicable"},"reason":{"type":"string","minLength":1,"maxLength":2048}}}]}`

### Invocation

| 字段 | 必填 | 格式约束 |
|---|---|---|
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| account_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalID"}` |
| created_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| finished_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalInstant"}` |
| frozen_input_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| parameters_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| method_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| processing_state | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ProcessingState"}` |
| validation_state | 是 | `{"enum":["not_evaluated","checking","passed","exhausted"]}` |
| attempts | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:runs#/$defs/Attempt"},"minItems":0,"maxItems":3}` |
| selected_attempt | 是 | `{"$ref":"urn:investment:spec:v1:runs#/$defs/OptionalAttempt"}` |
| sealed_output_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| next_chain_allowed | 是 | `{"type":"boolean"}` |
| terminal_failure_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| files | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:runs#/$defs/FileEntry"},"minItems":0,"maxItems":100000}` |
| retention | 是 | `{"$ref":"urn:investment:spec:v1:runs#/$defs/Retention"}` |
| baseline_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| expected_generation | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Count"}` |

### DailyManifest

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"const":1,"type":"integer"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| execution_date | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ExecutionDate"}` |
| timezone | 是 | `{"const":"Asia/Shanghai"}` |
| updated_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| invocations | 是 | `{"type":"array","items":{"$ref":"urn:investment:spec:v1:runs#/$defs/Invocation"},"minItems":0,"maxItems":100000}` |

### ProtocolRejection

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"type":"integer","const":1}` |
| rejection_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ID"}` |
| stage_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/StageID"}` |
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalID"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalID"}` |
| detected_at | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/Instant"}` |
| rule_id | 是 | `{"enum":["T01","T02","T03","T04","S01","S02"]}` |
| object_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/OptionalArtifact"}` |
| json_pointer | 是 | `{"$ref":"urn:investment:spec:v1:runs#/$defs/OptionalPointer"}` |
| reason_code | 是 | `{"enum":["invalid_attempt_state","invalid_manifest","retention_blocked","invalid_cleanup_state"]}` |
| reason | 是 | `{"type":"string","minLength":1,"maxLength":2048}` |
| owner | 是 | `{"enum":["controller","storage"]}` |
| action | 是 | `{"enum":["refuse_control_mutation","refuse_cleanup"]}` |

### RunResult

| 字段 | 必填 | 格式约束 |
|---|---|---|
| schema_version | 是 | `{"const":1}` |
| status | 是 | `{"const":"completed"}` |
| run_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| stage_id | 是 | `{"const":"SC01"}` |
| invocation_id | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/PathID"}` |
| attempt_no | 是 | `{"type":"integer","minimum":1,"maximum":3}` |
| next_chain_allowed | 是 | `{"const":true}` |
| sealed_output_ref | 是 | `{"$ref":"urn:investment:spec:v1:common#/$defs/ArtifactRef"}` |
| allowed_uses | 是 | `{"type":"array","uniqueItems":true,"items":{"$ref":"urn:investment:spec:v1:common#/$defs/Use"}}` |
