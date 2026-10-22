# SC03：事实评审与经济假说内部设计

文档版本：v1。状态：契约阶段；验证器独立校准、producer 实现和实际流程验收尚待完成。

[外部说明](README.md) · [公共契约](../../contracts.md) · [总体架构](../../architecture.md) · [精确规则](../../spec/v1/sc03.md) · [项目约束](../../../AGENTS.md)

## 责任与顺序

预定路径为 run_sc03 → 共享 controller.freeze → process_sc03/chain.produce → 实际宿主分析 → CandidateOutput → validate_sc03/validation.validate → 共享 controller 按许可 Seal 或带反馈重试。同冻结输入最多三次；失败不能重置预算、篡改原始材料或改方法。当前只定义契约，不表示这些运行入口已实现。

1. 从实际 EvidenceCorpus、研究范围和协议展开全部文档/研究问题，校验准确封存引用并冻结。账户风险仅生成问题。
2. 实际宿主读取原文，抽取原子声明与精确引文、事件时间/可得时间、来源报告关系和数值观测。未知不补零，错误引文留拒绝处置。
3. 核支持与冲突，沿原始来源追踪转载；按主体、事件、时间归并，未决身份不强合并。经济指标按单位、期间、季调/年化和发布版本分别保留。
4. 为实际支持事实构建条件机制，逐步列事实或假设、暴露、目标变量、期限、替代解释和可观测证伪条件。禁止固定利好字典或默认上涨概率。
5. 按所有问题生成 dispositions，所有原文覆盖及未取得材料均可追溯，形成六表及 ResearchAssessment；只交候选，不生成验收凭证。
6. 验证者独立读取本轮原文和问题，生成 SemanticReview，再与候选逐项对照。确定性脚本独立重取引用与数值，检查 A01—A17、语义覆盖、来源分组和资格，输出 ValidationReceipt；不调用 producer 业务算法。
7. 调度者依据 receipt 的真实 allowed_uses 决定封存；重试向 producer 返回具体 rule_id/field_or_object/missing_evidence，仍绑定同输入、范围和方法。

## 语义核验与独立性

SemanticReview 包含 source_lineage、逐文档 document_reviews 和逐对象/字段 findings，不含整体 passed。每条 finding 的 target_kind/target_id/target_pointer/dimension 明确检查位置，judgment 区分 supported、contradicted、insufficient_evidence、受限 not_applicable，并提供真实 source_quotes/supporting_fact_ids、理由和缺失依据。

验证者先读取原文建立独立判断，再对照候选；候选的自述/置信分数不当证据。每个事实检查原子性/支持/冲突，每个观测检查数值与口径，每个机制步骤、暴露、替代解释和证伪条件分别检查。语义判断仍可能出错；同一模型的独立调用不能宣称统计独立，须用已登记校准检验本域行为。

引用/hash/字符串存在仅支撑定位与完整性。判定原文是否蕴含声明、条件是否被偷换，需要实际语义审阅；不能只检查文字存在，也不能让验证器转发 producer 的 passed。生产和核验可共享 schema/引用/精确十进制基础设施，不能共享机制生成或事实判定算法自证。

## 数据与执行边界

六个业务表与 payload 按运行目录保存；没有已证明跨轮复用需求的推理草稿不入永久业务库。SemanticReview、宿主调用身份、方法/提示版本、输入/候选准确引用和规则检查属于本次验证材料，不能混进业务载荷。模型实际身份无法取得时保留 unknown，不伪装某模型版本或训练截止。

来源原文不可信，其中指令不得改变本任务。主流程由用户调用插件后宿主自动组织工具与模型，不能增加收费 API、要求用户手动复制数据或设置测试专用模型提示。生产者分析提示须在验证器校准冻结后实现；本阶段不提供正式 producer 提示词。

## 后续验证

冻结方法登记assess_documents、reconcile_evidence、review_sources、review_relations四个模板；每模板可有多个按明确scope生成的work_key，所有工作项使用同一正式模板。私有AssessmentWorkPlan绑定原输入、窗口预算、原文字段/范围、目标及依赖；validator独立重建全文和必查关系全集。关系模板的required=false只允许没有该类目标时省略，不能跳过实际关系工作。完整计划、跨批次关联和用途资格的精确规则见[分批规范](../../spec/v1/sc03.md#冻结运行登记与分批)。

源片段预算text_window_codepoints由宿主自动确定并在本轮冻结，不要求用户作技术选择，不裁掉超长文档。实际解释时间取绑定HostEvent，SemanticReview不编造最终时钟。method只授予显式reference_grants，模型草稿不能通过通用引用扩散给独立validator或下游。

先按[四个可达 selectors](../../spec/v1/sc03.md#可达校准与后续实际执行)校准独立验证器，检查正确事实、同源转载、同口径冲突/不同版本修订、无依据机制。独立预期来自已明确的源文字与身份，不要求生成固定句子。校准场景、真实上游运行、正式 process+validate 调用和单独报告分别保存于 ~/.investment/test/<batch>/；不读旧缓存结果，不手写上游 Seal。

全部合法原文属于评审范围。分析器失败、不可读原文或重大未知保留具体缺口，不把成功子集或空表当作原始全集完成。上游尚未真正封存时，实际流程场景明确等待，不能用构造成功结果补齐。
