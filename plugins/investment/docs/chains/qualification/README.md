# SC08：独立资格验证

文档版本v1。状态：仅契约/科学依据阶段，待根审核。尚未实现本链验证/流程脚本、正式宿主模板，未执行真实流程与四selector校准。机器格式可加载不是科学或金融验收通过。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [精确规范](../../spec/v1/sc08.md) · [内部流程](internal.md)

SC08评价冻结预测/更新与政策的科学证据、费用后优势、风险、现金与账本可行性，以及原始范围和检验治理。独立评价结果不用于改金额、改门槛或重选总体赢家。生产者只生成CandidateOutput；独立验证者读取本轮输入及原依据，按Q01—Q18重算并签ValidationReceipt；共享调度才可封存。

## 输入与输出

准确输入为SC01 TransactionContext、独立FundingPlan、SC04 InstrumentUniverse、SC05 DatasetBundle及ExperimentProtocol/独立物理验证视图、SC06 ForecastBundle和ModelRule、SC07 FrozenPolicy/完整PolicySpace/当前动作及ComparisonInventory。读取权由本轮真实上游Seal/用途和显式grant核对，不重跑上游业务验收。报告用途不自动允许数值评价。

UserMandate沿SC05 parameter_specs的真实用户/方法来源，由SC07 Objective、LossDefinition与ConstraintSpec中的准确parameter_id绑定，不建共享新包装。FundingPlan是本轮可投入上限，不是余额；SC01不供账户资金。历史默认SC05事前标准实验账户，只评价方法；今天持仓、名单、合同和费率不能回填旧origin冒称历史用户收益。

QualificationVerdict精确绑定verified_policy_ref、verified_actions_ref Dataset、verified_model_rule_ref和原Optional selected_rule_ref，必填评价期间/访问审计、原origin回放/真实模型执行、独立动作/账本/核对、预测指标、同账户基准配对、净优势区间、风险估计、现金检查、原范围复核、重复检验和假设报告引用，以及verdict、permission_scope、rejection_reasons、valid_until。完整50类定义/十表及元信息见[Schema](../../spec/v1/sc08.schema.json)和[registry](../../spec/v1/registry-sc08.json)；任何未知都保留原因，不填0、删除对象或换为hold。

## 方法与科学边界

每历史origin只读当时实际可得范围/身份/合同/市场版本和已成熟训练标签；完整冻结规则允许的rolling/expanding再拟合与原定重规划才可执行，不能根据最终验证成绩换特征、超参、方法或胜者。准确治理登记时间与经济信息可得时间分别核验。SC06预计经济期间的upper索引不是成交/估值/到账的精确物理时间。

资金/份额/批次费用及应收到账必须从原合同和交易事实独立重建；验证器不调用SC07或本链producer账本算法。政策和每个登记基准保持相同账户起点、资金贡献、CNY、期限与财富口径。标准实验结果不宣称真实用户收益。

净优势为逐origin Wpolicy-Wbaseline的等权均值（CNY）；损失L=B-W_H保留负值，CVaR按最坏1-beta质量及概率原子部分权重。区间沿SC05冻结moving_block_bootstrap_percentile，同origin完整财富差/政策损失/基准损失向量同步连续块重抽；l/B/随机性/指标/门槛和逐claim alpha在开放前登记。均值下界须严格超过经济门槛，CVaR上界不得超适用预算。统计confidence、CVaR beta、用户预算和工程重复数分别表达。

区间只是匹配依赖/稳定性/矩/尾部和统计量前提的条件渐近近似。l<=n、有限样本/B大、无显著诊断拒绝或固定N阈值均不能证明科学充分。ESS没有冻结估计量就unknown。已见资料后自适应修改不能只领新alpha再用同holdout；多重预算也不能把原来没有覆盖性质的bootstrap变成精确保证。预测coverage只用事前真实预测区间，不能拿事后均值区间替代。逐claim适用论证、成熟样本、缺失/重叠origin、尾部清单和知识污染限制必须保留。

原范围复核回到SC04原目录/分页/raw、来源任务、预测处置及SC07真实计算/Proof原依据；核U闭合、R客观依据、A=R、R外有据排除/独立非重大证明和全部实际风险。成功下载、模型ready、预算或producer confirmed不能缩分母或自证完整；有限观察不得取得原完整研究资格。

## 资格及未决依赖

Q01—Q18完整有序。critical={Q01,Q02,Q03,Q16,Q17}全passed才可reporting/evidence_review；current_account_review另需Q06/Q13/Q18；independent_evaluation需全部通过，并受全部AcceptanceRule.required_for约束。不授decision_advice或未来盈利保证，SC09另核当前发布。qualified必须完整范围及全规则通过；证据/区间不足与成熟证据支持的金融/经济失败分开，失败不靠换赢家补救。

真实跨轮重复检验provider归Task10；目前没有可消费的已冻共享API。本阶段只定义[精确消费提案](internal.md)，不注册不存在共享角色/schema，不建本链历史库，不造首轮空历史。缺provider时不开放独立资料，history_ref=unknown、明确Gap、撤回独立资格；可诚实完成可访问范围的缺口报告。需要前轮历史的selector目前不得伪造执行。

已实际阅读[Cawley/Talbot](https://www.jmlr.org/papers/v11/cawley10a.html)、[ETH报告70](https://stat.ethz.ch/Research-Reports/70.ps.gz)、[一般损失CVaR原文](https://sites.math.washington.edu/~rtr/papers/rtr187-CVaR2.pdf)、[ES块bootstrap原文](https://content.scirp.org/pdf/TEL_2018030615221974.pdf)及[NIST并集界](https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm)的相关原文范围；各自前提、用途及不能支持的结论详见[method.scientific_basis](../../spec/v1/method-sc08.json)。
