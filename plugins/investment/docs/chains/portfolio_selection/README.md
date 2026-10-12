# SC07：组合与资金方案选择

文档版本：v1。状态：外部业务契约设计，待实现与验收。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md)

本页定义本子链对外解决的问题、输入输出、科学方法和验收。内部步骤、适配器和分析器由子链自行编排，不能改变这里的字段含义、数值口径或失败语义。内部实现只生成 CandidateOutput，由独立阶段验证者签发 ValidationReceipt，调度者形成 SealedOutput。

## 外部业务定义

**问题。** 在同一账户、资金、期限、费用及风险口径下，选择完整决策规则和当前合法动作。

**输入。** SC01 TransactionContext、独立任务输入 FundingPlan 与 UserMandate、SC04 InstrumentUniverse、SC05 实验及交易合同、SC06 ForecastBundle、合法动作集合，以及原列表发现、金融取材审计与逐成员预测处置的准确引用。先搜索同类列表再比较，不能由调用者传入任意基金集合来代替 SC04 原分母。

**输出 FrozenPolicy。**

| 字段 | 类型与语义 |
|---|---|
| policy_id、policy_rule_ref、frozen_at | 完整规则、身份与冻结时间 |
| account_snapshot_ref、forecast_ref、experiment_protocol_ref、universe_ref | 决策所绑定的输入 |
| objective_ref、loss_definition_ref、constraints_ref | 目标、损失与约束定义及参数来源 |
| action_space_ref、baseline_definitions_ref、comparison_inventory_ref | 全部声明动作、基准与比较清单 |
| selected_rule_ref、current_actions_ref | 选定完整规则及本次精确动作 |
| wealth_paths_ref、cash_schedule_ref、baseline_evaluations_ref | 共同情景下财富、逐时现金和配对基准比较 |
| constraint_results_ref、solver_result_ref | 金融核对、求解状态、界限/最优间隙/容差 |
| rejection_reasons_ref、unresolved_items | 未选依据与重大未知 |

comparison_inventory_ref 使用 [公共 ComparisonInventory](../../contracts.md)：保留原始名单 U、候选/证据排除/未知、来源及预测处置、按冻结规则应比较的 R、实际计算的 A，以及每个未比较项、排除或非重大影响证明。A 从真实共同口径计算记录取得，计划提交求解器不等于已比较。

原同类名单不受下载、模型样本数或工程预算限制。客观准入、份额及币种口径、账户合同与合法动作约束可以影响 R 或政策集合，必须有事前规则和来源依据，并由独立验证者复核。缺资料或缺模型不能单独将成员从 R 排除；不能只比较 ready 子集而声明完整同类最优。金额精度定义有限合法动作与基金成员完整性是不同问题。

**模型。** 采用有约束的情景组合优化：以统一费用后财富为目标，显式约束尾部风险、现金、锁定、到账、集中度和用户登记的其他限制。多期规则只使用当时可见信息；本次建议仅发布当前动作，未来动作保留为条件规则。[Boyd 等：Multi-Period Trading](https://web.stanford.edu/~boyd/papers/cvx_portfolio.html)

推荐基础目标为 max E[W_H(a)]，a 属于冻结的合法动作或政策集合 𝒫；𝒫 绑定 action_space_ref/policy_space_ref，实际比较成员集另以 A 表示；风险约束采用明确损失定义下的终点 CVaR。CVaR 的情景形式：

$$
\operatorname{CVaR}_{\beta}(L)
=\min_z\left[z+\frac{1}{1-\beta}\sum_s p_s\max(L_s-z,0)\right].
$$

beta 是风险分位参数，用户风险预算与统计置信水平另行定义。终点 CVaR 不等于全程最大回撤或每期条件风险保证。[Rockafellar 与 Uryasev：CVaR](https://sites.math.washington.edu/~rtr/papers/rtr179-CVaR1.pdf)

财富定义固定 valuation_mode：

- mark_to_market：现金、持仓、待确认/应收资产和负债在终点的统一估值。
- cash_realized_by_deadline：目标期限内实际可交付现金；政策必须包含合法变现、费用与到账过程。

交易确认与到账状态读取 SC01；现金与完整资产口径从独立任务材料及产品资料核验。原始净值与分红现金可以共同核算；已含分红再投资的总回报序列不能再加同笔分红。基金已计入净值的运作费不再次扣除；申赎费、税、汇兑与终点变现假设按登记口径计入一次。应收款属于资产，但实际到账前不能支持购买。

基准至少包括保持现有持仓和保留可投资现金；全仓合法转现金若列为基准，须承担真实费用、锁定和到账。所有方案共享起始账户、外部资金计划、币种、期限和财富口径。

**内部链路。** 原分母和逐项处置 → 应比较集合及实际计算清单 → 账户/合同状态 → 合法动作集合 → 各方案财富与现金模拟 → 风险及约束 → 选择期比较 → 求解证据 → 完整规则和当前动作冻结。

**验收。** 核对 U 全体有处置、R 外有证据依据、A 是真实计算对象，完整比较须 A = R 且无重大未知。预算耗尽或漏算保留 limited 和未比较项，只支持实际集合内的结论，不满足完整目标。需要最优结论还须对应合法政策集合及求解证明；完整名单不代替求解证明。

金额与份额精度进入合法动作定义，不能靠发布时随意舍入改变赢家。标为最优须有对应集合与求解证据；预算内找到的方案只能标 best_found。真实与预测费用分开；未知费用不填零。不能利用独立验证结果重选。

冻结对象包括特征处理、训练与滚动更新规则、资金核算、费用处理、范围构建、再平衡和并列选择规则。今日静态动作不替代完整政策。风险不满足但可能减轻风险的方案属于另行登记的风险恢复政策，使用独立许可范围，不冒称普通调仓资格。

**失败路径。** 原名单未闭合、重大来源/预测未知、应比较成员漏算或必要金融条件不足时保留原范围、缺口和责任。可形成获准的限定研究，不通过删除成员、零费用或未到账资金制造合格方案；独立资格不足时不发布普通金额建议。

SC01消费边界：读取准确冻结的TransactionContext及其investment-run交易、持仓、pending引用，核对generation、ledger_digest、登记范围、完成依据和allowed_uses；不直接读取数据库latest替代输入。整笔确认份额决定持仓，卖出款项到账不再次扣份额。expected_due不等于已知实际到账时间；应收不得自动当成本次可用资金。缺费用或持有起点仅限制需要它的成本/持有期判断，不能猜0。不同买入trade可供持有期分析，单个trade没有分批确认/到账。有效消费结束前保留运行引用；清理后新建执行，从实际账本重新导出。
