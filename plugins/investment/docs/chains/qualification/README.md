# SC08：独立资格验证

文档版本：v1。状态：外部业务契约设计，待实现与验收。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md)

子链标识：SC08。说明目录：docs/chains/qualification/。代码包：尚未实现。精确规范：本链专用机器格式及验证规则待定义。

本页定义本子链对外解决的问题、输入输出、科学方法和验收。内部步骤、适配器和分析器由子链自行编排，不能改变这里的字段含义、数值口径或失败语义。内部实现只生成 CandidateOutput，由独立阶段验证者签发 ValidationReceipt，调度者形成 SealedOutput。

## 外部业务定义

**问题。** 冻结规则的预测、费用后优势、风险及金融可行性是否具有足够证据。

**输入。** FrozenPolicy、冻结模型/更新规则、封存独立验证资料、ExperimentProtocol、SC01交易/登记持仓上下文、独立FundingPlan/UserMandate与当前动作、各阶段验收凭证及历史检验记录，以及 SC04 原名单/DiscoveryAudit、SC05 SourceAcquisitionAudit、SC06 逐成员预测处置、SC07 ComparisonInventory 的准确引用。

**输出 QualificationVerdict。**

| 字段 | 类型与语义 |
|---|---|
| verified_policy_ref、verified_actions_ref、verified_model_rule_ref | 必须绑定准确被验证对象 |
| protocol_ref、evaluation_period_ref、test_access_audit_ref | 预登记协议、独立时段与访问审计 |
| evidence_kind | historical_simulation、prospective_simulation 或 actual_execution |
| predictive_results_ref、baseline_comparisons_ref | 预测诊断与配对费用后比较 |
| net_advantage_intervals_ref、risk_estimates_ref | 估计、区间、单位、适用假设与有效样本 |
| cash_feasibility_ref、ledger_reconciliation_ref | 现金、份额、费用、到账和交易核对 |
| comparison_scope_validation_ref | ArtifactRef；原列表闭合、全成员处置、R 的依据与 A = R、重大未知及风险保留的独立核对 |
| repeated_test_accounting_ref | 同方法族的已用检验记录与剩余许可 |
| verdict、permission_scope、rejection_reasons、valid_until | 结论、允许用途、拒绝原因和时效 |

**方法。** 选择后独立评价、逐时点规则回放、配对经济比较、依赖结构适用的统计区间及独立金融重建。样本指标和置信区间必须注明所支持的假设；工程重算证明计算合同，市场证据评价投资效果。

模型选择在有限数据上也会过拟合，必须隔离最终评价。[Cawley 与 Talbot：模型选择及评价偏差](https://www.jmlr.org/papers/v11/cawley10a.html)

**内部链路。** 原列表/处置/实际比较独立复核 → 冻结对象鉴权 → 验证资料开放 → 每个 origin 重建信息与合同 → 冻结规则生成动作 → 独立账本回放 → 基准配对比较 → 风险/误差证据 → 许可判定。

**验收。** 必要检查由预登记 acceptance_rules 决定，包括范围、预测证据、费用后比较、风险证据、金融可行性和检验治理；不得遗漏检查后整体通过。

范围检查必须回到原始目录、分页、身份和处置证据，核对闭合与 U = C ∪ E ∪ K，再逐项核实来源、预测状态及真实比较记录。完整比较须验证 R 的客观依据、A = R、R 外逐项排除或非重大影响证明，以及所有实际持仓和在途风险保留。生产者生成的名单、自哈希、ready 标记或“已完成”声明不能单独作为证明。

原名单未知、应比较成员漏算或重大缺口未处理时，不能授予完整同类比较、跨范围最优或普通完整研究资格。有限集合仅支持其声明范围的观察；另行登记的风险恢复许可不替代普通完整目标。非重大影响证明须绑定本次目标和约束，从原证据独立核实，不能用模型分数或人工标签放行。可采用费用后优势的置信下界超过预定经济门槛、风险上界不超过预算的判据，但区间方法和适用条件必须先登记，不能事后选择。

验证区间不能用于修改特征、模型、基金范围、目标、门槛或胜者。预先冻结的滚动更新规则可在每个 origin 使用此前已成熟资料，不能借已见验证结果修改规则。失败后的修改属于新试验，需要新的未接触证据或预登记重复检验处理，不能清空历史次数。

今天的持仓、现存基金目录和当前费率回放历史，不是整链历史样本外证明。实际来源版本、身份、曝险和合同必须逐时点可得；LLM 历史知识污染无法排除时限制对应证据用途。

**失败路径。** insufficient_evidence 与 failed 分开；验证只返回结论和缺口，不重选赢家或改变金额。没有未来盈利保证类许可。
