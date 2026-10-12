# SC05：时点数据与实验协议

文档版本：v1。状态：机器契约已根审；独立验证器及源审核模板已实现待根审核冻结，流程与完整真实验收尚未实现/执行。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md)

子链标识：SC05。说明目录：docs/chains/research_dataset/。验证器代码包：src/investment/chains/research_dataset/；入口scripts/validate_sc05.py。精确规范：[SC05规则](../../spec/v1/sc05.md)、[Schema](../../spec/v1/sc05.schema.json)、[注册表](../../spec/v1/registry-sc05.json)、[方法与用途](../../spec/v1/method-sc05.json)。

本页定义本子链对外解决的问题、输入输出、科学方法和验收。内部步骤、适配器和分析器由子链自行编排，不能改变这里的字段含义、数值口径或失败语义。内部实现只生成 CandidateOutput，由独立阶段验证者签发 ValidationReceipt，调度者形成 SealedOutput。

## 外部业务定义

**问题。** 主动取得原同类范围和实际风险对象所需的金融资料，构建真实历史信息集，定义预测目标、比较口径和独立评价方法。资料取得、来源就绪与历史训练资格分别核对。

**输入。** SC01 交易与持仓上下文、独立任务目标与约束、SC03 经济观测、SC04 当前冻结范围及 universe_history_ref、data_build_spec_ref、method_policy_ref。

原始金融版本、费用与权益合同、交易日历和市场资料是可选复用资料，不要求调用者预先取得。SC05 对缺项自行取材；复用仍须核对准确字节、实体身份、来源、时点和合同适用范围，不能只接受 ready 标记或人工填入的旧日期。

data_build_spec_ref 登记各资料能力的来源规则、必要字段、当前和历史时点请求、解析规则及工程预算；实际实现版本在执行记录中绑定。取材清单覆盖 SC04 原始发现名单和强制风险对象，保留每项假说、行业或暴露所需的市场和基准任务。不能将成功取材、样本成熟或模型可运行的成员改写为原始基金范围。

**输出 DatasetBundle。**

| 字段 | 类型与语义 |
|---|---|
| current_market_snapshot_ref | 当前报价、净值日期、可得性与市场状态 |
| observation_vintages_ref | 所有相关观测版本与来源 |
| universe_history_ref | 沿用并审计 SC04 的逐 origin 范围，不以当前目录替代 |
| source_acquisition_audit_ref | SourceAcquisitionAudit 的 ArtifactRef；逐对象资料任务、原始证据、全部尝试、未处理项、来源处置及受影响用途 |
| asof_panel_ref | 按历史 origin 构建的当时可得特征面板 |
| label_events_ref | outcome、origin_at、outcome_end_at、label_available_at、label_status |
| contract_vintages_ref、calendar_ref | 当时有效的费用、申赎、分红、锁定与到账规则 |
| availability_audit_ref、data_gaps_ref | 版本可得性审计、缺失项及允许用途 |
| split_manifest_ref | 训练、选择、独立验证分区及访问权限 |
| experiment_protocol_ref | 冻结的实验协议；也是事前 method_policy_ref / scope_ref 的准确绑定 |
| equity_events_ref | 真实分红、拆并、确权/到账及再投资版本，不能由累计净值猜出 |
| training_view_ref、selection_view_ref、independent_validation_view_ref、current_view_ref | 分别落盘的受限视图；下游按用途显式授权，不能递归展开审计 raw |

SourceAcquisitionAudit 的公共字段见 [公共契约](../../contracts.md)。基金任务涵盖 NAV、权益和分红、费用、申赎、精度、锁定、确认与到账、交易日历及当前市场状态；行业/暴露任务绑定假说和实体，不从基金名称猜测基准代码。

每个原始成员和必要风险对象均有逐能力的资料处置：ready、proven_not_applicable 或 unknown。资料不足、来源冲突和采集失败保持 unknown；不适用必须有规则和原文证明，不能将未取得资料解释为客观不可交易。SC04 已有证据排除的成员也保留任务及有据不适用处置，不必强行下载无关资料。

来源就绪不等于预测合格，历史版本不合格不自动否定当前观察用途。必要字段无法取得时保留具体 Gap，不补零、不插造价格、不删除成员或实际风险。来源审计只声明资料处置，不能自行修改 SC04 准入或签发科学资格。

观测行至少含 entity_id、metric_id、observation_period、value、unit、currency、version_id、published_time、available_time、availability_basis、retrieved_at、raw_ref、supersedes_version_id。

日历行另必填scheduled_valuation_period（完整TimeEvidence）：在所用origin/C之前有源的预计估值经济期间，不是未来NAV。保留日期/区间完整边界、精度、来源时区与依据，上界仅索引经济期间；缺来源、时区或当年可得证据保持unknown及Gap。有源明示无计划估值才可not_applicable，非交易日不足以证明，也不能借此删掉或顺延原请求。真实历史标签继续使用已观察期间；采用预计安排界定期限时须同一origin可得版本和同一观察口径。

published_time 与 available_time 为 TimeEvidence；value 为 OptionalValue<DecimalString>，currency 与 supersedes_version_id 的不适用状态使用 OptionalValue。asof_panel 行至少含 origin_at、entity_id、universe_version_id、feature_values、source_version_ids、availability_bound_at。每个特征关联准确来源版本、单位与可得性界限；身份、暴露、合同与文本解释也通过同一时点检查。

ExperimentProtocol 必填：protocol_id、target_definition、horizons、origin_rule、split_rule、purge_embargo_rule、universe_rule、baseline_definitions、wealth_definition、cost_execution_rules、model_search_space、scenario_method、metrics、acceptance_rules、statistical_assumptions、search_budget、repeated_test_policy、randomness_policy、parameter_specs、registered_at。

**方法。** 来源取材与原始字节保全、Point-in-time 版本选择、量纲分析、确定性时间对齐与预登记实验。ALFRED 的实时版本定义说明了“今天掌握的历史值”和“过去当时已知值”的区别。[FRED Real-Time Periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)

**内部链路。** 原成员及风险范围 → 逐对象资料任务 → NAV/权益/费用/申赎/日历/市场取材 → 身份、单位和合同核验 → 来源处置 → 可得时点审计 → 各 origin 信息集 → 标签成熟审计 → 分区与方法冻结 → 数据清单。

**验收。** 任务和处置完整覆盖原始成员及必要风险对象，每个已执行请求、失败、重试和未处理项均可复核。原始来源支持 NAV 日期、权益、费用和合同口径；不能用空值、零或删除对象制造就绪。SC05 不改变 SC04 原同类分母；身份、暴露或列表冲突交回主调度处理。

origin=t 的特征只使用 availability_bound_at 为 known 且不晚于 t 的版本；日期级证据采用协议登记的保守边界，不虚构时分。面板 entity_id 必须属于 universe_version_id 绑定的该时点合格范围；资料未核的实际持仓继续作为风险对象，不能冒充训练成员。标签在训练截止前须成熟且可观察，最新修订不回填历史。学习型标准化、插补和特征选择由 SC06 在训练折内拟合。

**协议冻结。** 数据和方法不能根据独立验证表现修改。SC05 分离训练/选择资料与封存验证资料，模型与策略生产者只读取前者。split、隔离长度、样本资格和阈值的确定规则须事前登记。

分区以(label_id,fold_id)为主键，标签绑定sample/target/horizon；DataView按真实label_ids过滤，同一sample不同horizon分别成熟/排除。选择标签区间必须与独立评估区间不交叠、可得界不晚于事前选择信息界及首个独立origin；不得挪边界或借fold重命名绕过。冻结滚动更新可用此前成熟资料，但不能据已见独立结果重新选方法。

**当前与历史边界。** C=information_cutoff_at，当前报价、特征与模型更新标签逐项证明可得界<=C；每个历史 origin=t 使用其自己的可得信息，训练标签还须在该折 fit_cutoff 前成熟。NAV日期不能替代发布/可得时间，后修订不回填旧origin。默认事前标准实验账户为明确正CNY资本、空仓且无外部追加，资本来源与实验假设登记；它只评价方法，不冒称用户余额或真实收益。真实当前账户仍由SC01与独立FundingPlan提供。完整关系见SC05规则。

**目标与分布支持。** 算术总回报用有证据的单位NAV及权益事件重建，或明确的分红再投资指数；累计净值不自动等同后者。R=W_H/W_0-1且R>=-1；identity目标下预测均值/场景低于-1即该经济输出不可用，不截断、不删除坏场景后重归一化、不偷换log目标。金融与统计资格仍由对应独立验证者核对。

情景直接给出相对同一C的各horizon累积回报，所有asset×horizon共享同一历史origin残差向量；网格仅C锚点和真实h端点，不能把累计收益串乘或补造中间价格。零财富后不能无注资回升。Metric算法/单位/输入闭合，ESS未支持保留unknown、不当门槛。重复检验使用有限K等分或真实全局j的可求和预算，逐claim派生置信；预算分配不修复holdout自适应复用或把近似bootstrap变成精确覆盖保证。

**失败路径。** 部分失败保留实际尝试、原范围和补核任务；处理结束不代表来源、历史或预测资格通过。当前资料可用于观察，不必同时具备历史训练资格；缺历史版本或成熟标签时不产生合格预测输入。缺必要业务字段时形成真实控制失败和 Gap，不补造正常 payload。历史模拟、前瞻模拟与真实成交分别登记。
