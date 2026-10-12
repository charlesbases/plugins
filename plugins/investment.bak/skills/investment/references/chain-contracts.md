# 业务子链契约

此接口定义每一步的业务输入、输出和完成条件，必须先完成协议定义与冻结，再设计子链和调度器实现，再通过每个子链的独立测试，最后运行真实完整流程。`scripts/chains/contracts.py` 和辅助 DTO 测试只验证协议结构与绑定；它们不代表子链实现验收或真实投资目标已完成。

契约验证只校验调用者明确传入的 JSON，不读取持仓、`latest` 对象、网络或可变状态，不修复生产者输出，不执行交易。源内容、账户重放、科学验证和费用风险验证仍由既有独立 engine gates 执行。

## 顺序和输入输出

第一步没有业务输入，读取本次账户记录；缺必要事实时只询问未知字段。随后每步接受本次调用上一子链的**完整 envelope**，不是另外寻找旧对象。每步 `payload.account_context` 原样传递第一步的事实投影。

| 阶段 | 上游输入 | 类型化输出容器 | 业务内容 |
|---|---|---|---|
| `account` | 无 | `account_context` | 本金、可接受本金亏损比例、已到账现金、持仓代码与份额；固定 TT/CNY |
| `news_collection` | AccountContext | `news_material` | 近期国内和国际原文、真实出版与获取时间、动态来源及待补范围 |
| `news_assessment` | NewsMaterial | `assessed_news` | 原文对应受影响行业、可解释经济机制、无影响时的明确依据 |
| `fund_universe` | AssessedNews 与账户投影 | `frozen_universe` | 本次全 TT 目录、行业对应完整同类成员分母、当前 H、候选 Q、未知及有依据排除 |
| `fund_sources` | FrozenFundUniverse | `source_bundle` | H/Q 各代码准入和证据、现金基线、未知与排除、完整 engine CAS 引用 |
| `model` | SourceBundle | `model_result` | 源合格范围的 Elastic Net、时间顺序独立校准、费用、CVaR 和现金比较结果 |
| `recommendation` | ModelResult 与账户投影 | `recommendations` | 对每个当前 H 明确增持、减持或保持，必要时纳入科学比较过的同类 Q |
| `report` | Recommendations | `report` | 实际结论、来源覆盖、同类比较范围、投资目标是否已达成 |

工程分页、抓取预算或一个成功下载不是全球新闻分母或最佳基金的科学资格。完整目录中的基金无需全部可买或具备模型资格；完整同类成员必须保留，并明确归为候选、未知或有源依据的排除。**未知不自动失败，也不自动通过**：观察报告仍可完成；声称完成投资目标时，必须有完整同类成员分母、未知代码的源依据和准入说明，以及真正的独立比较资格。

## 固定 envelope

机器定义为 [business-chain.protocol.json](business-chain.protocol.json)，科学定义为 [scientific-contracts.md](scientific-contracts.md)。三份定义必须一并冻结内容身份；下文与机器定义不一致时先修定义，不能按现有草稿实现倒推放宽协议。

`schema_id = investment_business_chain_v1`，`schema_version = 1`。字段固定如下：

```json
{
  "schema_id": "investment_business_chain_v1",
  "schema_version": 1,
  "chain_id": "stable-current-invocation",
  "stage": "account",
  "status": "complete",
  "processing_complete": true,
  "stage_complete": true,
  "investment_goal_achieved": false,
  "lineage_refs": [],
  "protocol_sha256": "<frozen business protocol SHA256>",
  "implementation_identity": "<frozen implementation identity>",
  "account_ref": {"sha256": "<64 lowercase hex>", "size": 123, "path": "objects/.../<same SHA256>"},
  "account_hash": "<same SHA256 of complete current account snapshot>",
  "input_output_hash": null,
  "output_hash": "<SHA256 of canonical payload>",
  "payload": {},
  "source_refs": [],
  "operation_receipts": [],
  "qualification_receipts": [],
  "qualifications": {
    "source_integrity": false,
    "fact_qualification": false,
    "point_in_time": false,
    "predictive_validation": false,
    "comparison_scope": false,
    "recommendation_qualification": false,
    "trade_ready": false
  }
}
```

`account_ref` 绑定完整当前账户快照，包括账户账本和本金/风险事实 lineage；不是现金估值来源。`account_hash` 必须等于该快照 CAS 的 SHA256。首次缺事实且尚无账户快照时二者允许为 null。后续所有阶段必须保持同一账户引用和 hash。

`input_output_hash` 是上一完整 envelope 的 canonical SHA256，首步为 null。`output_hash` 是本步 `payload` 的 canonical SHA256。不同 `chain_id`、不同账户、跳步、替换旧 envelope、修改 payload 都不能通过绑定校验。

`status` 只允许 `complete | awaiting_user | awaiting_analysis | needs_evidence | failed`。必须区别三个维度：`processing_complete` 表示一次技术处理已经结束并封存真实结果或诊断；`stage_complete` 表示该子链定义的**业务后置条件**得到满足；`qualifications` 表示具体独立证据和科学资格。任何一个维度都不自动提升其他维度。

`stage_complete` 不能简单等于“返回了 JSON”或“执行结束”。fund_sources 处理完完整 H/peer 的 source dispositions 可以 `processing_complete: true`；当前风险和必要比较输入必须合格或被独立源准入排除，重大源缺口不能使 `stage_complete` 为 true。model 的科学业务后置条件要求实际拟合、完整源合格范围比较与独立校准，不足时只能技术处理结束而科学业务未完成。严格主目标遇到未完成上游不能继续把它当成业务已通过输入。观察报告可以交付并明确 `goal_unmet`，但不能冒称八个科学子链全部通过。

| 状态 | 明确定义 | 调度器行为 |
|---|---|---|
| `complete` | 本步定义的业务后置条件已经满足，必要结果和独立验收齐全；明确源未知仅在本步后置条件本身允许完整处置未知时保留 | 调度器核本步业务后置条件和真实独立验收后，把此完整 envelope 原样交下一步 |
| `awaiting_user` | 第一子链缺本金、损失比例、现金或持仓的必要确认 | 返回且只显示未知必要字段的问题，不调用后续子链 |
| `awaiting_analysis` | 原文材料已备好，尚无通过原文支持核验的行业分析 | 调度者在当前任务读取原文并补分析；不把分析工作或模型参数转嫁给用户，不把空分析当 complete |
| `needs_evidence` | 尚不能形成本步必要业务对象，如没有真实目录引用、没有可解析上游对象 | 返回具体缺证据动作；调度者按真实原始预算补证并重验，不能填示例数据继续主链 |
| `failed` | 请求/引用/账户/哈希绑定冲突、独立校验拒绝、工程操作无法产生有效结果等实质错误 | 保留原失败操作与原因，不能标记本步或投资目标通过；只允许既有预算内的明确重试或新调用 |

未知风险已被明确核查并记录，与根本无法形成本步业务输出不同；业务对象形成又与科学业务通过不同。源处置的完整输出不等于必要源资格满足或模型实际比较完成。技术输出真实不足结果时，保留 `processing_complete` 和未完成的业务后置条件，不把预期拒绝或正确 unknown 响应说成实际投资业务已满足。无法形成必要业务对象时，非 complete 的类型容器可为 `{unavailable: true, required_actions: [{action: ...}], diagnostics_ref?: <actual CAS>}`；它允许真实 partial，不允许冒充完整对象或获得科学资格。

`trade_ready` 在此研究链始终 false：结构校验和建议的执行精度资格不会授予自动交易权限，也不保证未来效用。

## DTO 必要字段

嵌套对象允许既有 engine 元数据和原始 CAS 引用，不把大模型矩阵复制进每个 payload。金额、份额、损失比例均是精确十进制字符串。

- `account_context`: `currency: CNY, platform: TT, principal, loss_tolerance, cash, holdings`。未知必要字段为 null，持仓确认空仓为 `[]`。`payload.missing_fields` 按 `principal, loss_tolerance, cash, holdings` 顺序列出未知字段，`payload.questions` 为 `[{field, question}]`，恰好每个未知字段一个问题。已知字段不重复问，不要求 strategy spec 或算法参数。
- `news_material`: `window_start, cutoff_at, documents, coverage`。documents 为 `[{document_id, source_ref, source_id, url, title, published_at, retrieved_at, version_id?, ...}]`；保留日期精度和时区，不把发布日期或净值日期猜成调用日，同文档的不同原始版本不合并成假时间。coverage 含 `domestic_source_ids, international_source_ids, pending_source_ids, global_coverage_known, source_scope_complete`。pending 保留可继续输出观察结论，不得因工程处理完成而声称覆盖合格。新闻域必须事前明确、可审计且业务上足够广，说明材料相关性、重要性和反证覆盖；不要求全球每个网页下载，也不能用 maxURLs、固定来源或 top N 定义足够广的域。
- `assessed_news`: `material_hash` 绑定上一输出；`industry_links: [{industry_id, mechanism, document_ids, facts, counterevidence, uncertainties, direction, time_horizon, economic_observations_ref}]` 只引用本次 documents，并显式列出事实、方向、作用时间、经济观测、反证和未明处。无影响时用 `industry_links: [], no_material_industry_impact: true, reason`。
- `frozen_universe`: `currency, platform, catalog_codes, catalog_ref, holding_codes, candidate_codes, unknown_codes, excluded_codes, peer_groups`。peer_groups 为 `[{group_id, industry_ids, member_codes, membership_complete, source_refs}]`，关联本次所有受影响行业。候选/未知/排除完整覆盖每个 peer member；不能删除未知或未取到的成员。每个排除为 `{code, reason, source_refs}`。
- `source_bundle`: `holding_codes, candidate_codes, qualified_codes, unknown_codes, excluded_codes, by_code, cash, evaluation_at, historical_availability_refs, dealing_terms_refs`。代码分区不重叠且保留全部 H 和 peer 分母。`by_code[code]` 至少含 `{status: qualified|unknown|excluded, source_refs}`，未知/排除还需 `reason`；可含 `engine_refs`。cash 为 `{currency: CNY, amount: <actual confirmed cash>, source_refs}`。evaluation_at 是实际证据评估时刻，不用它替代各源的出版、净值、历史可得或费用适用时点；历史可得和交易条款各有具体来源引用。
- `model_result`: `holding_codes, candidate_codes, unknown_codes, excluded_codes, compared_codes, cash_compared, method, execution_status, model_fit_executed, comparison_gaps, evidence_refs`。`execution_status` 只允许 `not_run | insufficient_data | unqualified | qualified | failed`。evidence_refs 是包含 `method_result_ref, bundle_ref, calculation_ref, readiness_ref, family_results_ref, selection_ref` 的明确对象，每个值是本次真实 CAS 或 null，缺引用必须有 comparison_gaps 说明，不能填示例结果。真正科学比较时必须覆盖全部 source-qualified 同类范围，不能只挑几个成功取源的对象；compared_codes 和 cash_compared 仅由实际 calculation 得出。若 engine 没有运行模型，必须用 `model_fit_executed: false, compared_codes: [], cash_compared: false`，预测资格为 false，科学业务后置条件未满足。H 未核时保留 H，不编造调整。method 描述严格 PIT 输入、成熟标签、Elastic Net、仅按时间顺序训练选择、实际费用后净财富、CNY 现金、CVaR、冻结优胜者后的独立校准；未运行时明确是 requested method，方法声明自身不构成实际运行或资格。
- `recommendations`: `holding_codes, candidate_codes, model_hash, scope, actions`，actions 为 `[{code, action: increase|reduce|hold, reason, amount_CNY, shares, fees_CNY, evidence_refs, qualification}]`。amount_CNY/shares/fees_CNY 仅允许从真实科学及费用、现金、风险、舍入证据得出精确十进制字符串；未核为 null 并解释原因，不以 0 代替未知。evidence_refs 保留真实建议依据，qualification 保留独立建议验收结果和范围。每个 H 必须有明确动作；未知 H 可 hold 观察并解释限制，但它不是科学合格的“保持最优”结论。增减只对实际已科学比较并独立验证的代码。
- `report`: `conclusion, recommendations_hash, model_hash, coverage, goal_achieved, report_markdown, limitations`。coverage 为 `{source_scope_complete, comparison_scope_complete, source_refs}`。结论与中文 report_markdown 连通本次新闻→行业→同类基金科学比较→当前 H 调整，实际金额为 CNY 并带依据，limitations 保留所有影响结论的缺口。目标字段必须与 envelope 一致，目标为 true 时所有必要资格必须有调度器认证的真实独立 proof，且实际 model/recommendation 也分别合格。

## 工程回执与独立资格

operation_receipts 行至少含 `{operation_id, operation, status: completed|partial|failed}`；可含 `result_hash, source_ref, engine_refs` 和既有操作信息。这只说明工程操作，不能升级事实、PIT 或预测资格。

qualification_receipts 必须包含：

```json
{
  "operation_id": "concrete-independent-validator-operation",
  "status": "passed",
  "independent": true,
  "validator_scope": "specific independently checked evidence and scientific scope",
  "source_ref": {"sha256": "<proof SHA256>", "size": 123, "path": "objects/.../<proof SHA256>"},
  "chain_id": "<current envelope chain_id>",
  "stage": "<current envelope stage>",
  "account_ref": {"sha256": "<current account SHA256>", "size": 123, "path": "objects/.../<account SHA256>"},
  "account_hash": "<current envelope account_hash>",
  "input_output_hash": "<current envelope input_output_hash>",
  "output_hash": "<current envelope payload hash>",
  "protocol_sha256": "<current frozen protocol SHA256>",
  "implementation_identity": "<current frozen implementation identity>",
  "validator_id": "<scheduler trusted concrete independent gate>",
  "qualifications": ["predictive_validation"]
}
```

operation_id 必须对应一条 completed 工程回执。proof 必须绑定同一链、同一阶段、当前账户、精确上游和本步输出。纯 contract 仅验证这些绑定和 proof 引用结构；**不验证 proof 的独立性或科学真实性**，不能以自声明 `independent: true`、抓取文件 hash 或代码 hash 代替真正独立 gate。调度器必须确认 proof 来自定义中明确受信任的独立 gate、对应实际公开操作的具体验收，重新核查其真实来源、结果和作用范围；不接受生产者自己声称验证独立。任何未独立通过的资格保持 false。

## 纯验证 API

本节定义职责接口，不冻结草稿代码的当前 signature。协议冻结后，子链设计和实现必须显式加入 processing/business 状态、lineage 与冻结身份，不能继续使用旧“执行结束就是 complete”参数语义。

`make_envelope` 接受明确本步结果、完整上游、账户引用、固定控制身份、处理/业务状态和原始回执，构造深拷贝并检查所有绑定；资格默认全部 false，不根据加工结束自动提升业务完成。

`validate_envelope` 检查精确本步输入输出的 schema、hash、账户、lineage 和冻结身份；`validate_chain` 检查从 account 开始的有序链或显式 observation partial 传递。其结构验收 `status: passed` 只指 DTO 校验，不证明实际业务后置条件满足，也不认证科学资格。严格目标判定归调度器真实独立验收及最终 goal gate。

`validate_ref` 校验 immutable CAS 的 SHA256/size/path，支持按 SHA256 引用分块且分块 byte size 相加必须等于全对象。它不读取 CAS 内容；实际字节和受信独立 gate 回执认证由调度器校验。

`tests/test_chain_contracts.py` 的所有输入和 proof 是明确标注的 synthetic fixtures，仅用于验证绑定与拒绝机制，不算真实新闻、市场证据或模型资格。

## 每步证据、缺口与独立验收

每个输出同时带业务对象、精确原始来源引用和工程操作引用。下载时间、HTTP 成功、目录计数、代码版本 hash 与文件存在只属于工程证据，不单独成为事实或科学资格。

| 阶段 | 必须传递的实际来源 | 需要独立检查的业务输出 | 允许保留的真实缺口 |
|---|---|---|---|
| `account` | 本次完整账本、本金/损失偏好确认、风险修订 lineage 的 account_ref | 本金与损失比例来自实际确认；到账现金与每个 H 来自当前记录；缺项恰好对应必要首问 | 既往收益、旧购入成本或来源估值未知可保留；必要账户事实缺失则 awaiting_user |
| `news_collection` | 本次动态来源目录、真实原文版本/正文 CAS、实际出版与获取时间、分页总分母 | 国内/国际本次实际来源、窗口和原文范围；原文与引用字节一致；待补来源和未知全球分母不丢 | pending sources、读取失败或时间窗口完整性未知可透明记录；没有必要原文对象则 needs_evidence |
| `news_assessment` | 本次 collection、document/version refs 和实际 news_assess result_ref | 每个事实与机制由当前原文支撑；行业关联和方向可解释；不从关键词猜收益 | 缺分析 awaiting_analysis；无法确证的机制保持未建立，不能伪造行业影响 |
| `fund_universe` | 本次原文行业分析 result_ref、完整 TT catalog_ref、issuer/benchmark 同类成员原文、pending scopes ref | 行业相关源范围、所有同类成员、每个 H、候选/未知/源排除；未取得成员不删；声明完整的组确为完整成员分母 | 源未明成员/身份/准入保留 unknown；无目录对象则真实 partial；完整处理不等于最佳选择 |
| `fund_sources` | 同一 discovery result_ref、逐代码原始 NAV 与日期/版本、原始发行人及 TT 费用合同、账户现金 ref、历史可得 refs 与实际 evaluation_at | 所有 H 与候选准入状态、净值日期真实性、完整费用与申赎约束；全部当前风险和必要比较输入合格或被独立源准入排除；重大 unresolved sources 不满足本步业务后置条件 | NAV、费用、现金权属或历史 PIT 缺口明确 unknown；处置结束只使 processing_complete 真，源不足时 stage_complete 仍假；有源准入排除保持文档说明 |
| `model` | 完整 source bundle、实际研究与 market result refs、方法及 actual calculation/calibration refs | 实际 Elastic Net 拟合、时间顺序训练与独立校准、成本、全合格同类净财富/CVaR/现金比较；当前 H 全保留 | 未拟合须明确 model_fit_executed false，科学比较代码为空；历史证据不足不能降门槛或编历史时间 |
| `recommendation` | 同一 actual model result_ref 与不变账户 ref，实际 action analysis/建议验证 ref | 每个 H 的增减/保持、同类候选选择及费用风险依据；动作精度资格与自动执行权限分开 | 未科学比较的 H 保持观察与风险未知；不编造增减金额或声称最优 |
| `report` | 同一 actual recommendations/model refs、覆盖与独立 gate proof refs | 结论对应实际 H 建议、源覆盖与同类范围透明；核心目标完成与观察输出结束分开 | 可诚实完整输出观察结论而 goal false；不可把 partial、源缺口或 awaiting_analysis 写成全目标 pass |

来源与科学验收应由与生产者分开的校验函数执行，对原始字节/账户事实/模型结果重新核查。收到 `status: passed` 必须同时读其明确 `validator_scope`；DTO 绑定、源复现、账户事实、PIT、预测校准和建议精度不是同一个资格。任何工程回执与宣告字段都不能代替其需要的独立验证。

## 调度器协议

调度器是主链的唯一协调者，负责执行顺序、输入传递、独立验收、持久日志、恢复和最终目标判断。默认 `requested_goal: investment_recommendation`，严格要求每步业务后置条件与独立验收；观察仅是 `delivery_mode: observation`，不是换成第二个较弱目标，必须明确 `status: goal_unmet`。它不把技术诊断或观察响应当作科学主链通过。子链函数只处理明确收到的上游 envelope；工程 helper 可复用原公开操作，但不能在下游另选 `latest`、旧 review、旧 discovery、旧 fee inspection 或旧 model。

控制上下文包含 `data_root, plan, chain_id, protocol_sha256, implementation_identity`，属于调度配置而非业务输入。科学定义也须绑定版本及内容 SHA256；理论定义、用户风险、源合同和明确内部预登记分别说明参数来源。协议和实现身份用于重放一致性，不能成为科学资格或未来收益保证。

调度器输入 DTO 是 `{requested_goal, chain_id, control_context, business_input: null}`。主链第一步不接受额外业务输入；host 的新闻分析是基于本次原文的内部提交，不是额外用户问卷。

调度器输出 DTO 必须包含 `{requested_goal, chain_id, status, delivery_mode, current_stage, chain_outputs, last_good_output_ref, failed_boundary, goal_evidence}`。status 只允许 `goal_achieved | goal_unmet | awaiting_user | awaiting_analysis | failed`；delivery_mode 只允许 `qualified_recommendation | observation | pending`。目标通过须提供真实 goal_evidence；观察输出仍保留失败业务边界及最后真正通过输出，不能把它们重新包装为通过。

失败 DTO 必须包含 `{stage, phase, rule_path, input_ref, reason, attempt, retryable, required_actions}`。phase 只允许 `input_validation | execution | output_validation | independent_qualification | publication`。每次失败保留实际输入和预算消耗，不能吞掉错误、静默跳步或以最新对象代替。

1. 创建本次稳定 chain_id，执行无业务输入的 account 子链。它冻结完整当前账户 account_ref/hash。原账户已知事实不重复问；必要事实未明时停止后续调度。
2. 在执行下一步前，检查上一 envelope 的 schema、账户和输出 hash，确认是本次 chain_id 的正确上一步。调用下一步时直接传递这一个完整 envelope。
3. 子链生产真实业务对象并保留全部必要分母、未知和源排除。所有金融生产仍经现有 public `pipeline.run`，输出 result_ref 包含该实际 public result 和原 automatic validation，不重造 engine result。当前链前缀完整 envelope 的 CAS lineage_refs 随输出保留，使 model/report 可读到本次新闻机制、完整同类分母和更早验收，不必用最新 selector 补丢失的证据。
4. 独立验证器接受本步明确上游和实际输出；重读具体 operation_id/result_ref/source bytes 核查，不扫描最新对象。它返回单独的 proof CAS、具体资格、作用范围和本次输入输出绑定；失败不由生产者自行修复成 pass。
5. 调度器调用前写 inflight，完成 envelope 绑定、业务后置条件和独立 proof 校验后，封存 output 和验收 receipt 再交下一步。严格主目标只有实际 stage_complete 才继续；未完成时记录阶段、精确输入 reference、具体规则路径、失败原因和实际消耗的 attempt。分析或补源由调度者在当前授权任务处理，并遵守原操作预算，不能靠新 operation namespace 无限重置验证失败。若补证仍未满足目标，可在显式 observation delivery 中传递已验证的 partial 诊断至观察报告；status 和 stage_complete 保持原未完成状态，投资目标明确 goal_unmet，并保留主链未完成位置。必要账户事实和 host 原文分析不允许绕过。
6. 所有后续阶段沿用第一步账户 ref/hash。执行前检查账户、本金、损失偏好或计划是否改变；变化触发明确 stale snapshot，新调用重新冻结，不能混入新的持仓金额。
7. 同链恢复必须使用确切封存的 inputs、outputs、operation IDs、protocol/implementation identities 与原预算，不重新取 latest。新的正常调用使用新 chain_id 和近期窗口，并读取已有确认账户而不重复必要事实首问。
8. 只有实际独立通过的资格可写 true；完整原文、完整同类成员及有源准入解释仍不足以证明未来获利。最终 `investment_goal_achieved` 根据真正的模型、实际当前 H 建议和明确覆盖证据共同判断，不能由工程阶段数量或一份 report 文件决定。

实现与验收顺序固定为：**冻结全部 I/O 与调度器协议 → 依契约设计各子链和调度器 → 每个子链独立测试全部通过 → 调度器串联测试通过 → 当前真实调用的完整运行及证据验收**。任何 draft API、合成 DTO 测试或单阶段工程复现只属于其对应范围，不预先宣称真实完整链路成功。
