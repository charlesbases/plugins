# SC09：建议形成与发布许可

文档版本：v1。状态：外部业务契约设计，待实现与验收。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md)

子链标识：SC09。说明目录：docs/chains/recommendation/。代码包：尚未实现。精确规范：本链专用机器格式及验证规则待定义。

本页定义本子链对外解决的问题、输入输出、科学方法和验收。内部步骤、适配器和分析器由子链自行编排，不能改变这里的字段含义、数值口径或失败语义。内部实现只生成 CandidateOutput，由独立阶段验证者签发 ValidationReceipt，调度者形成 SealedOutput。

## 外部业务定义

**问题。** 将通过验证的动作表达成用户可执行建议，并确认发布时仍满足原前提。

**输入。** FrozenPolicy、QualificationVerdict、发布时交易/登记持仓版本核对、独立资金计划与用户约束有效性核对、市场与费用有效性、申赎状态及提交窗口。

**输出 RecommendationSet。**

| 字段 | 类型与语义 |
|---|---|
| advice_id、decision_at、published_at、valid_until | 建议身份与时效 |
| account_snapshot_ref、market_snapshot_ref、policy_ref、qualification_ref | 准确绑定决策和资格 |
| decision_kind | rebalance、no_action、risk_recovery 或 observation_only |
| advisory_orders_ref | 已验证当前动作及执行条件 |
| position_dispositions_ref | 每项持仓和候选的建议、理由、资格及未知 |
| funding_conditions_ref | 等待到账/追加的条件，不作为当前可支配资金 |
| publication_checks_ref、limitations | 发布核对和限制 |

建议订单行至少含 advisory_order_id、instrument_id、platform、currency、side、subscription_cash_amount、redemption_shares、lot_allocations、fee_estimate、reference_nav、reference_nav_date、estimated_result、precision_contract_ref、execution_conditions、reason、evidence_refs。

side 决定金额/份额字段互斥：申购以用户实际支付金额为指令，并说明是否含费；赎回以份额为指令，保留批次分配及持有期费率。非本方向字段明确 not_applicable。参考申购份额、预计净到账金额与费用标为 estimated，不冒充确认成交。

subscription_cash_amount 与 redemption_shares 使用 OptionalValue<DecimalString>；执行方向的指令值必须为 known 且大于零，另一字段为 not_applicable。fee_estimate 与 estimated_result 中的各估计量使用 Estimate；reference_nav 为 OptionalValue<DecimalString>，reference_nav_date 为 OptionalValue<Date>，必须绑定可核市场来源。实际确认的无动作由 no_action 表达，不生成金额为零的交易指令。

**方法。** 已验证动作的确定性映射、来源合同精度校验、账户版本核对与有效期相交；不重新优化。

**内部链路。** 资格及对象绑定 → 账户/市场/合同新鲜度 → 精度与动作一致 → 指令及条件表达 → 发布许可。

**验收。** 当前建议只能复制被验证动作。合并展示保留批次和核算明细；改变动作金额或经济效果需要新决策和验证。valid_until 为必要条件有效边界的最早值；已过期只允许作为历史产物。

decision_kind=no_action 须有明确验证依据；未知金额保持 null 及原因。observation_only 不生成交易指令。risk_recovery 只能取得其单独政策的许可。资金条件达成后更新独立资金计划并重新核验；涉及基金卖出到账时登记交易到账事实，再重算受影响方案，不发布假定已到账的订单。

**失败路径。** 发布前发现账户、风险、费用或市场条件变化，阻断原建议，不在原对象内替换新值；回到受影响阶段重新计算。

SC01消费边界：读取准确冻结的TransactionContext及其investment-run交易、持仓、pending引用，核对generation、ledger_digest、登记范围、完成依据和allowed_uses；不直接读取数据库latest替代输入。整笔确认份额决定持仓，卖出款项到账不再次扣份额。expected_due不等于已知实际到账时间；应收不得自动当成本次可用资金。缺费用或持有起点仅限制需要它的成本/持有期判断，不能猜0。不同买入trade可供持有期分析，单个trade没有分批确认/到账。有效消费结束前保留运行引用；清理后新建执行，从实际账本重新导出。
