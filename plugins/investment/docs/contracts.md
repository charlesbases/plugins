# 公共输入输出契约

文档版本：v1。状态：公共设计规范；机器格式启用范围见强格式入口，规范本身不代表运行验证已通过。

[总览](../README.md) · [总体设计](architecture.md)

公共类型与交接包遵守[项目约束](../AGENTS.md)的职责划分：上一子链对其输出负责验证，下一子链直接消费本轮已验证输出；当前验证脚本仅验证自身输出，可以回溯本轮输入和原始依据重算当前结果，不重复执行上游业务验证。第一子链由入口登记用户事实及完成必要结构检查。

本页说明各子链共用的类型及外部交接。精确字段、封闭 JSON Schema、登记表、跨对象规则和失败控制以 [v1 格式规范](spec/v1/README.md) 为准；各子链 README 说明职责。内部适配器、分析器和步骤的私有约定不加入公共外部契约。已登记机器格式及待定义范围统一见强格式索引；子链职责说明不能代替机器规则或实际验收。

## 4. 文件格式与公共契约

### 4.1 载体选择

每条子链交接一个 JSON 契约包，引用零至多个数据表、原始证据与模型产物。文件载体不决定可信度，schema、语义和证据规则共同决定结果可用性。

| 载体 | 用途 | 约束与理由 |
|---|---|---|
| JSON + JSON Schema | 控制信息、业务对象、范围、参数、引用、缺口与验收结果 | 支持嵌套对象与明确类型；用结构校验和业务验证器共同验收 |
| Parquet | 净值、经济观测、特征面板、预测、情景和回放路径 | 列式分析数据；声明列类型、DECIMAL 精度、时间类型和主键 |
| CSV + schema manifest | 小型交换与人工核查 | 明确编码、分隔与转义规则、类型、单位、空值和主键；不隐含类型推断 |
| 原始 HTML/PDF/JSON/其他响应 | 原文与合同证据 | 保留原始字节、实际来源、版本和获取记录；正文解析结果不能替代原始对象 |
| Excel | 人工审阅与报告附件 | 记录源包哈希、范围和生成时间；代码及精确值保留文本列；修改后重新生成输入并校验 |
| Markdown/HTML/PDF | 解释性报告 | 引用准确建议及研究产物，不能作为后续自动计算的唯一依据 |

JSON Schema 约束结构，跨字段守恒、来源资格和统计有效性由业务验证器检查。[JSON Schema Validation](https://json-schema.org/draft/2020-12/json-schema-validation)

Parquet 的列式格式与逻辑类型适合研究面板；精确十进制必须登记 precision 和 scale。[Parquet Overview](https://parquet.apache.org/docs/overview/)、[Logical Types](https://parquet.apache.org/docs/file-format/types/logicaltypes/)

CSV 采用明确的行列与转义规范，并附语义 schema。[RFC 4180](https://www.rfc-editor.org/info/rfc4180/) Excel 会处理数字精度和前导零，正式身份与精确值需保留文本表示。[Microsoft：Format numbers as text](https://support.microsoft.com/en-us/excel/format-numbers-as-text)

### 4.2 公共类型

下表定义文档中的类型。未列为可空的字段必须存在且非 null；每个业务对象仅接受本版本定义的字段；内部组件字段不能直接加入公共对象。

| 类型 | 定义 |
|---|---|
| ID | 非空稳定标识；不通过名称或显示顺序推断身份 |
| Instant | 带时区的 RFC 3339 时间；系统统一比较，保留源时间精度 |
| Date | YYYY-MM-DD 经济归属日期，不自动补为午夜时刻 |
| DecimalString | 有限规范十进制字符串；禁止 NaN、Infinity、千分位及隐含单位 |
| Hash | 规范字节的 SHA-256，64 位小写十六进制 |
| ArtifactRef | artifact_id、schema_id、schema_version、media_type、uri、sha256、byte_size |
| DatasetRef | ArtifactRef 加 table_schema_ref、row_count、primary_key、units、null_policy |
| Gap | rule_id、field_or_object、reason、owner、required_evidence、affected_uses |
| ParameterSpec | name、value、unit、source_kind、basis_ref、determination_rule、determined_at |
| OptionalValue | value、status、reason；status 为 known、unknown 或 not_applicable |
| TimeEvidence | OptionalValue 中的已知 value 含 lower_bound、upper_bound、precision、timezone、basis_ref；界限为 Instant，保留源精度与可得性证据 |
| Estimate | value 为 OptionalValue，另含 value_basis=estimated、assumptions_ref、as_of、evidence_refs；不得表达为已确认成交 |

所有引用均必须解析到准确的不可变字节。未知媒体或 schema 不得通过宽松解析进入业务计算。uri 负责定位，sha256 与 byte_size 负责核对引用；不能以 latest 或人工文件名替代版本绑定。

金额、份额和净值用 DecimalString；确定性金融核算用 Decimal 或等价定点表示。统计估计可以使用浮点，但必须有限，并记录计算精度与适用容差。最终动作按来源规定的 money_step、share_step、rounding_rule 检查，中间核算不提前舍入。JSON 数值的互操作精度受接收程序影响。[RFC 8259](https://www.rfc-editor.org/info/rfc8259/)

null 只能在契约允许的 OptionalValue 中表示未知或不适用，并附原因。数值零是已确认或已计算的零。空数组表示在声明范围内确认没有成员；不知道持仓不能写空数组，不知道费用不能写零。输入缺失是契约错误，字段存在但业务事实未知则是有明确用途限制的业务状态。

可得时间统一使用 available_time: TimeEvidence。availability_bound_at: OptionalValue<Instant> 是按冻结协议从可得性证据导出的保守上界；仅在 known 且该界限不晚于 origin_at 时允许历史使用。源日期、经济期间或后来抓取时间不能自行倒推为更早的可得时刻。无法获得有限可信上界时保持 unknown。时间证据的 precision 枚举为 instant、date、month、quarter、year 或 interval；未知时 value 为 null 并说明缺口。

### 4.3 输入、候选输出与封存输出

输入包为 StageInput；生产者交付 CandidateOutput；验证者生成 ValidationReceipt；调度者将两者绑定成 SealedOutput。不存在生产者自行授予下游许可的路径。

| 对象 | 必填字段 |
|---|---|
| StageInput | contract_id、contract_version、run_id、stage_id、invocation_id、decision_at、information_cutoff_at、scope_ref、input_refs、parameters_ref |
| ExecutionRecord | run_id、stage_id、invocation_id、input_ref、processing_state、started_at、finished_at、failure_ref |
| CandidateOutput | contract_id、contract_version、run_id、stage_id、output_id、created_at、processing_state、input_refs、scope_ref、method_ref、payload_ref、datasets、evidence_refs、gaps |
| ValidationReceipt | validator_id、validator_version、run_id、stage_id、input_refs、candidate_output_ref、validated_at、checks、overall_verdict、business_result、allowed_uses、blockers |
| SealedOutput | run_id、stage_id、output_id、candidate_output_ref、validation_receipt_ref、sealed_at |

input_refs 的每项包含 role 与 ArtifactRef 或 DatasetRef，由对应 role 的登记类型限定。datasets 与 evidence_refs 可为空数组，但必须满足阶段规定的必要项。不适用 method_ref 的阶段引用其固定业务规则，不省略责任依据。SC01 精确 role、顺序、身份绑定与引用定位见 v1 规范。

ExecutionRecord 记录 pending、running、completed 或 failed；started_at、finished_at 为 OptionalValue<Instant>，failure_ref 为 OptionalValue<ArtifactRef>。pending 的 started_at 为 unknown，开始执行后才为 known；实际完成或失败后记录真实 finished_at。正常 CandidateOutput 的 processing_state 必须为 completed。未产生候选时返回强格式 Failure，不伪造正常 payload 或候选验收凭证；不以前置 InputCheck 代替“流程后验证当前输出”的职责。每个子链首次加两次重试共三次；耗尽后全部后续流程终止，包含 SC10，由主调度控制出口说明失败原因。

SealedOutput 的结构示意如下，尖括号为占位符，不是真实证据：

~~~json
{
  "run_id": "research-001",
  "stage_id": "SC06",
  "output_id": "forecast-001",
  "candidate_output_ref": {
    "artifact_id": "forecast-candidate-001",
    "schema_id": "investment.forecast_bundle",
    "schema_version": 1,
    "media_type": "application/json",
    "uri": "<存储定位>",
    "sha256": "<64位内容哈希>",
    "byte_size": 1000
  },
  "validation_receipt_ref": {
    "artifact_id": "forecast-validation-001",
    "schema_id": "investment.validation_receipt",
    "schema_version": 1,
    "media_type": "application/json",
    "uri": "<存储定位>",
    "sha256": "<64位内容哈希>",
    "byte_size": 800
  },
  "sealed_at": "2026-10-13T15:00:00+08:00"
}
~~~

采用事前登记的规范 JSON 编码计算哈希，拒绝重复键及非有限数值。引用哈希位于外部封存对象中，不把自身哈希字段纳入自身计算。二进制文件按实际字节哈希；表的语义行序如参与计算，必须在 manifest 中规定。

每项检查包含 rule_id、verdict、reason、evidence_refs；数值检查增加 actual、threshold、unit、tolerance。verdict 为 passed、failed、insufficient_evidence、not_evaluated 或 not_applicable。必要检查缺失或未执行时不得整体通过；not_applicable 必须有协议允许的理由。

overall_verdict 固定为 passed、partial 或 failed。passed 要求全部必要检查通过或有协议允许的不适用结论；partial 表示结构与引用有效，但某些用途的证据不足或尚未评价；failed 表示必要检查失败。business_result 固定为 usable、observation_only、blocked 或 rejected：结构/引用错误为 rejected；必要金融前提不成立为 blocked；已确认材料可解释而决策证据不足为 observation_only；指定用途的必要条件全部通过才为 usable。拒绝产物只作控制记录，不作为业务依据。下游最终按具体 allowed_uses 与必要检查判断，不仅检查 overall_verdict。

allowed_uses 使用固定枚举：evidence_review、hypothesis_research、current_account_review、pit_training、selection_comparison、independent_evaluation、decision_advice、reporting。许可只对被验收的准确对象、范围和有效期间成立。

### 4.4 五层强校验与失败结果

以下层次用于核对本子链输出，不构成重新验证上游业务输出的前置流程。验证器独立以本轮实际输入和原始证据为依据，检查输出合理、有效且有事实支持；不修改输入、证据、候选或阈值迎合结果。入口必要解析、精确值类型及运行边界仍须按已定义格式执行。

| 层次 | 检查内容 | 不通过时 |
|---|---|---|
| 结构 | 当前输出必填项、类型、枚举、数值与日期语法、未知字段 | rejected，不提交或交接后链 |
| 关系 | 主键、外键、输入引用、哈希、账户版本、范围分区、祖先身份 | rejected，不允许拼接或替换 |
| 来源与时间 | 原文字节、引文、版本可得性、合同适用期、源精度和时效 | 相关用途 blocked 或 observation_only |
| 金融与业务 | 资金/份额守恒、费用、到账、锁定、合法动作、范围含义 | 相关决策 blocked；不补造事实 |
| 统计与科学 | 成熟标签、选择/验证隔离、模型假设、比较证据、风险与检验预算 | decision_advice 禁止，保留限定观察 |

processing_state 位于 ExecutionRecord 与正常 CandidateOutput，仅描述执行；业务状态读取 ValidationReceipt.business_result，使用许可读取同一凭证的 allowed_uses。技术完成不推出预测有效或金额合格。

契约错误必须拒绝；证据不足可以形成结构正确的观察产物。后者保存 Gap，说明影响的用途和补证责任，不把资料缺失改写为客观不可交易、无风险或模型已否定。

无法获取或证实的数据可以不采用，但保留原对象范围、真实尝试、弃用依据、缺口及影响。按事先定义且有依据的统一规则处置，不按个别对象、测试名称或期望结果硬编码放行/跳过。丢弃不可信数据不能改变原始任务范围、候选分母或成功标准；事实、派生值和模型推断分别表达，证据不足时不能以推测值或默认常数伪造通过。

来源链采用实体、处理活动、责任主体与派生关系的记录思路，借鉴 [W3C PROV-DM](https://www.w3.org/TR/prov-dm/)。可追溯性支持核查与重建，来源本身仍需评审。

每条子链的业务产物使用上述外壳与公共引用类型。大表使用 DatasetRef，小对象使用 ArtifactRef；具体输入、输出和业务验收由对应子链 README 定义。SC08 策略科学资格评价不替代其他子链的局部独立验收。

### 4.5 范围发现、金融取材与实际比较的公共审计对象

下列业务审计均使用 ArtifactRef 引用 JSON 对象，大表用 DatasetRef；contract_version 与 schema_version 为 1，文档标识为 v1。审计由相应子链生产并接受独立阶段验证，不是内部适配器的私有返回值。所有必填引用须存在；未知业务事实使用 OptionalValue 并记录 Gap，不用缺字段代替未知。

**DiscoveryAudit。** SC04 的 discovery_audit_ref 指向此对象。

| 必填字段 | 类型与语义 |
|---|---|
| audit_id、run_id、universe_ref、scope_ref、discovery_spec_ref | ID 与 ArtifactRef；universe_ref 指向 U 的不可变名单/规则资料，不反向引用尚未封存的 SC04 外壳 |
| original_list_refs、query_tasks_ref、attempts_ref | ArtifactRef 数组及 DatasetRef；原目录/列表字节、分页/游标/查询父子任务、每次真实请求 |
| membership_provenance_ref、possible_members_ref、unprocessed_scope_ref | DatasetRef；成员与原条目/同类组映射、可能成员、未处理分页/区间/来源域 |
| closure_checks | 检查数组：rule_id、verdict、reason、evidence_refs；按同类组记录 |
| completeness_status、gaps | complete 或 unknown；Gap 数组 |

complete 必须由独立验证者复核原列表的来源覆盖域、列表含义、分页结束、跨来源身份合并和未解析条目处置，才能取得完整范围用途。哈希只证明字节绑定，U = C ∪ E ∪ K 只证明对 U 的分区；两者都不证明 U 是完整同类列表。未知成员仍属于原名单，未取得的范围不伪造成员代码。成员行至少有 member_id、entity_id: OptionalValue<ID>、source_entry_refs、peer_group_refs；身份未定时依来源条目稳定定位。

**SourceAcquisitionAudit。** SC05 的 source_acquisition_audit_ref 指向此对象。

| 必填字段 | 类型与语义 |
|---|---|
| audit_id、run_id、universe_ref、mandatory_assets_ref、data_build_spec_ref | ID 与准确 ArtifactRef/DatasetRef；绑定原成员及风险范围 |
| acquisition_tasks_ref、attempts_ref、raw_refs | DatasetRef、DatasetRef、ArtifactRef 数组；逐对象/能力任务、每次请求、取得的原字节 |
| source_dispositions_ref、pending_tasks_ref、gaps | DatasetRef、DatasetRef、Gap 数组；来源处置、未完成项及影响用途 |

任务行至少有 task_id、object_ref、capability、requested_time_scope_ref、source_rule_ref、task_state、attempt_refs。task_state 为 pending、running、completed、failed 或 cancelled，不推导业务就绪。处置行至少有 object_ref、capability、disposition、rule_ref、evidence_refs、reason、affected_uses；disposition 为 ready、proven_not_applicable 或 unknown。ready 须有必要来源证据；proven_not_applicable 须有规则和原文依据；未取得、冲突、预算耗尽或解析失败为 unknown。每次尝试保存任务、来源、真实时刻和结果，失败和重试不删除。工程预算不能改变原分母，不适用处置不能静默改变 SC04 准入。

**ComparisonInventory。** SC07 的 comparison_inventory_ref 指向此对象，SC06 的逐成员预测处置为其前置证据。

| 必填字段 | 类型与语义 |
|---|---|
| inventory_id、run_id、universe_ref、discovery_audit_ref、source_acquisition_audit_ref | ID 与准确引用，绑定原列表及取材审计 |
| comparison_rule_ref、forecast_ref、policy_space_ref | ArtifactRef；同类可比口径、预测及合法动作/政策集合 |
| member_dispositions_ref、required_members_ref、actually_compared_ref、uncompared_members_ref | DatasetRef；对 U 全覆盖的处置、按规则应比较成员 R、真实比较成员 A、R 中尚未比较的成员 |
| exclusion_proofs_ref、materiality_proofs_ref、execution_evidence_ref | DatasetRef/ArtifactRef；有据排除、未知项非重大影响证据、实际计算记录 |
| completeness_status、gaps | complete 或 limited；Gap 数组 |

每项成员处置保存 member_id、entity_id、peer_group_ref、scope_status、source_status、forecast_status、comparison_status、reason、rule_refs、evidence_refs、affected_uses。未确认 entity_id 使用 OptionalValue；各状态使用对应权威子链的枚举，comparison_status 为 compared、excluded、unknown 或 uncompared。

U 不变；R 根据事前登记的可比规则及独立核实的处置从 U 形成，不能根据下载成功、成绩或预算选取。A 必须从实际计算及共同口径结果核验。完整比较要求 A = R 且没有尚未解释的可能进入 R 的重大未知；每项未进入 R 的成员均有客观排除或针对本任务目标的非重大影响证明。缺资料或缺模型不能单独成为这种证明。风险对象另行全覆盖，不因不属于 R 而删除。

非重大影响证明必须绑定目标、约束、范围及原证据，由独立验证者核实为什么该项未知不会改变本次比较资格或结论，不能以人工标记、低评分、低权重或生产者声称替代。无法证明则仍为重大 Gap。limited 只支持实际集合的限定结果，不满足完整同类研究目标；complete 也不自动证明求解最优或投资有效。

### 4.6 根目标证据与总结果

根目标验收是主调度的汇总责任，不新增第十一条业务子链，也不代替十链的局部验证。任务开始时冻结 goal_rule_ref，绑定研究目标、渠道/产品域、配置新闻范围、完整比较要求、所需科学/金融证据及结论强度；不能因为部分链失败而把完整目标改成有限比较。

**GoalEvidence。** 独立于子链生产者及建议选择者的根目标验证者读取准确封存产物与原证据，签发此对象；主调度记录和绑定其结论，不能自行置为通过。

| 必填字段 | 类型与语义 |
|---|---|
| evidence_id、run_id、validator_id、validator_version、validated_at | ID、版本和真实 Instant |
| goal_rule_ref、scope_ref | ArtifactRef，准确本次目标和范围 |
| account_ref | OptionalValue<ArtifactRef>；准确封存交易上下文引用（不代表余额或完整账户资产），未取得为 unknown 并记录必要事实缺口 |
| stage_output_refs、stage_validation_refs、control_record_refs | ArtifactRef 数组；所有实际取得的封存输出、验收凭证、失败/待办控制记录 |
| discovery_audit_ref、source_acquisition_audit_ref、comparison_inventory_ref、report_validation_ref | OptionalValue<ArtifactRef>；未取得须为 unknown 并有对应 Gap |
| checks、gaps、pending_work | 必要检查数组、Gap 数组、仍可在本次继续的 Gap 数组 |
| goal_status | achieved、unmet 或 pending |

必要检查至少覆盖：任务/账户/范围/祖先准确绑定；配置新闻范围的真实覆盖与必要证据；原同类列表闭合；U 全体逐项处置及全部持仓/在途风险保留；取材与时点资格；R 的独立合理性和 A = R 的真实计算证据；模型、风险、费用后比较、资金与动作资格；所请求结论强度的求解证据；建议发布前提；SC10 实际报告字节和上游语义一致性。按 goal_rule_ref 允许的不适用检查须有可核理由，不能将完整列表、真实比较或必要金融检查声明为不适用以通过。

- achieved：所有必要检查 passed 或有规则允许的不适用，没有影响完整目标的重大 Gap，准确账户引用为 known，所需十链封存产物及验收引用齐全，且 SC10 内容验收有效。合格“保持/不操作”也须有完整比较、风险与发布证据，不能因无动作跳过验证。
- unmet：已判定本次完整目标不能完成，例如不能在本次授权内恢复的必要检查失败、必要证据不可取得，或最终只能交付观察/有限比较。仅等待未来标签成熟或真实收益也属于当前目标 unmet。阶段检查失败后仍可在本次修复或重生成的情形先保持 pending，不以失败记录自动覆盖可继续判据。
- pending：尚需必要用户事实，或仍有本次可继续的来源、分析、核对等工作，且没有已确认使本次目标无法达成的失败。逐项列责任与所需证据；后续继续后产生新的根证据，不改写历史结论。

报告交付状态不作为科学通过证据。有效内容的 delivery_failed 可与 achieved 并存；交付成功的观察报告可与 unmet 并存。报告内容预核对或最终验收失败时不能 achieved。风险恢复政策只在其独立许可范围内使用，不能替代普通完整研究目标。未来盈利不是根目标的可验证通过条件。

**ResearchResult。** 总链路向调用者返回的不可变 JSON 契约包。

| 必填字段 | 类型与语义 |
|---|---|
| contract_id、contract_version、result_id、run_id、created_at | 固定契约身份、版本 1、ID 和真实 Instant |
| goal_rule_ref、scope_ref | 本次准确绑定的 ArtifactRef |
| account_ref | 与 GoalEvidence 一致的 OptionalValue<ArtifactRef> |
| goal_status、goal_evidence_ref | 与独立 GoalEvidence 一致的状态和 ArtifactRef |
| report_bundle_ref | OptionalValue<ArtifactRef>；指向已封存 SC10 输出，未取得为 unknown 并说明原因 |
| required_actions | Gap 数组，沿用根证据中的补证或待办责任 |

总结果不能仅引用 report_document_ref 或报告标题来表示通过。正常报告路径中，根验证者读取 SC10 SealedOutput 和实际验证凭证；报告中的前期资格不能代替最终根结论。

尚未取得必要账户事实、阶段失败或报告预核对/最终验收失败，因而没有有效 SC10 封存输出时，根验证者仍读取已完成阶段前缀和真实失败/待办控制记录，生成 pending 或 unmet 的 GoalEvidence 与 ResearchResult。未取得账户或有效报告引用明确为 unknown 并有 Gap；不补造报告、封存对象或完整研究证据。这条控制分支不能生成 achieved。

引用构建顺序：上游原证据/候选/验收/封存 → 报告字节及预核对 → 真实交付记录 → SC10 候选/最终验收/封存 → GoalEvidence → ResearchResult。ReportBundle 不引用最终 GoalEvidence 或 ResearchResult；审计对象不反向引用包含自己的尚未封存外壳，避免循环引用和循环哈希。

SC01的TransactionContext封存证明本轮整笔交易状态及登记份额归并已验收，许可以实际ValidationReceipt.allowed_uses为准；账户记录归属是account_id，不代表完整账户资金。TransactionFacts只含source_record_ref和base_generation；实际基线由控制器从SQLite读取并冻结。Context含generation/ledger_digest及本轮trades/positions/pending的investment-run准确引用，不依赖历史数据库Context。有效消费结束前保留对应runs；清理后不承诺旧引用可读，不能用最新账本重建旧字节。资金计划与用户约束独立输入，后续金融和最终建议资格分别验收。
