# SC10：报告交付

文档版本：v1。状态：外部业务契约设计，待实现与验收。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md)

子链标识：SC10。说明目录：docs/chains/reporting/。代码包：尚未实现。精确规范：本链专用机器格式及验证规则待定义。

本页定义本子链对外解决的问题、输入输出、科学方法和验收。内部步骤、适配器和呈现组件由子链自行编排，不能改变这里的字段含义、数值口径或失败语义。内部实现只生成 CandidateOutput，由独立阶段验证者签发 ValidationReceipt，调度者形成 SealedOutput。

## 外部业务定义

**问题。** 准确展示原文、事实、假说、基金名单与比较、账户风险、建议、阶段资格及限制，并记录报告的真实交付结果。

**输入。** 主调度提供所有实际完成上游阶段的 SealedOutput 准确引用、实际取得的 TransactionContext、RecommendationSet 或观察产物、Gap、验证记录及失败控制记录。引用须保持本次任务、账户版本、范围、祖先关系、字节哈希和用途许可，不用最新对象补齐或替换。失败控制记录只说明失败，不作为业务结论依据。

**输出 ReportBundle。**

| 字段 | 类型与语义 |
|---|---|
| report_id、generated_at、decision_at | 报告身份与真实生成、决策时点 |
| input_bundle_refs、account_ref、scope_ref | 实际采用的封存研究依据、准确账户与配置范围 |
| report_document_ref、review_workbook_ref | 中文报告 ArtifactRef；审阅附件为 OptionalValue<ArtifactRef> |
| conclusion、advice_summary_ref | 获准报告结论及对应建议或观察依据 |
| news_sources_ref、industry_research_ref、fund_comparison_ref | 可核原文、行业机制与基金名单、逐项处置及实际比较视图 |
| coverage_ref、qualification_summary_ref、limitations | 实际覆盖、阶段资格及作用范围、未知和限制 |
| next_required_actions | 待办、补证责任、必要用户事实与内部研究工作 |
| delivery_status | delivered 或 delivery_failed，仅表达真实报告交付结果 |

fund_comparison_ref 的视图保留原始发现名单、冻结同类名单、实际计算比较名单，以及候选、证据排除、未知和未比较明细。处置绑定规则、来源及原因；实际比较名单来自真实计算记录，不从计划、采集成功或报告排名推导。

完整清单在正文或随报告交付的可核附表中保留，不能仅展示排名靠前成员而隐藏分母。未取得的名单或结果明确表达 unknown 和 Gap，不用空名单表示确认没有成员。观察路径中未取得的 advice_summary_ref、industry_research_ref、fund_comparison_ref 等业务引用使用 OptionalValue；只对确实取得且具备 reporting 许可的对象使用 known。account_ref 与 scope_ref 保持准确必填，缺必要账户引用时仅形成失败控制记录。

**方法。** 来源驱动的确定性呈现与跨产物语义核对。LLM 可以组织文字，但名单、排名、金额、份额、费用、范围和资格须逐项核对封存输入。报告组件不检索新证据替换研究输入，不重新优化或产生比较结果。

**内部链路。** 输入及缺口汇总 → 事实/假说/估计/条件分列 → 完整名单、实际比较、账户和建议展示 → 内容核对 → 报告字节 → 独立内容预核对 → 真实交付 → ReportBundle 候选 → 独立 SC10 验收与封存。

**验收。**

- 新闻原文版本、配置窗口和来源范围、实际覆盖及未知分别展示，有限采集不扩大成全球覆盖。
- 原始发现名单、冻结同类分母、逐项处置与实际比较名单准确对应。有限比较只能按实际集合说明结果，不写成完整同类最优；未知或未比较成员不删除。
- 每项持仓及在途风险保留。正式建议逐项沿用动作、依据、资格、金额、份额、费用及条件；未取得建议时只展示已确认事实、风险和缺口，不补造零金额或合格无动作。
- 参考净值、预测份额、预计到账及费用保留估计性质，不冒充成交。未知费用、权益和金额不写零，不重复扣除已净计费用。
- 报告不产生排名、不改变建议或资格，不把观察写成最优、技术完成写成科学通过。必要内容缺失、引用错配或新增结论不能通过核对。
- 交付前独立预核对读取实际字节及准确输入，不代替最终 SC10 验收。delivery_status 来自实际交付记录，生成草稿或保存文件不证明已交付。

ReportBundle 不反向引用最终 GoalEvidence 或 ResearchResult，不预判完整研究目标。正常报告路径中主调度在 SC10 封存后调用独立根目标验证者汇总十链证据，再形成 GoalEvidence 与 ResearchResult；最终 goal_status 在 ResearchResult 展示。报告交付、完整研究目标和未来真实投资结果分别表达。内容有效且实际交付失败可封存 delivery_failed 报告；交付失败不自动否定独立核实的研究目标，也不自动证明其达成。

**失败路径。** 表达与输入不一致时基于相同输入重生成、重新核对；研究或金融错误返回责任阶段，不改文字掩盖。预核对未通过不交付，不包装成正常 ReportBundle。真实交付失败保存 delivery_failed 和原因。最终验收失败保留已经发生的交付事实、失败边界及补核要求，不修改历史记录伪造成功。没有有效报告封存时，由根验证者读取已完成前缀及控制记录返回 pending/unmet，相关报告引用保持 unknown，不能因此丢失总结果出口。
