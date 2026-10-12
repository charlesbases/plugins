# SC09：建议形成与发布许可

文档v1；插件0.0.1。独立验证器基线已接受，producer、标准入口与正式publication_assessment提示词已落盘，待fresh生产流程复审及真实业务验证。四个完整selector与真实Host尚未执行，基础数学检查不能代替业务资格。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md) · [精确规范](../../spec/v1/sc09.md)

SC09将已冻结且获得适用许可的当前首步动作映射成用户可理解指令，并核对发布所需前提。只有独立验证者签发ValidationReceipt、控制器形成SealedOutput后，后链才按许可消费。完整规则、引用权限、字段、单位、未知语义和四个事前selector见[Schema](../../spec/v1/sc09.schema.json)、[registry](../../spec/v1/registry-sc09.json)、[method](../../spec/v1/method-sc09.json)。

输入是准确SC01 TransactionContext、独立FundingPlan、SC05 DatasetBundle、SC07 FrozenPolicy、SC08 QualificationVerdict和宿主自动组装的PublicationContext。本链核本轮已收到/已登记事实及真实来源；不宣称查到无法访问的外部账户，不计算余额或增加对账职责，不要求用户技术JSON或再次确认已提供事实。新事实只能阻断已不适用建议并返回原责任方，不能改写冻结上游。

RecommendationSet保留advice_id、decision_at、published_at、publication_checked_at、valid_until及端点、account/market/policy/qualification/publication_context准确引用，decision_kind为rebalance、no_action、risk_recovery或observation_only。它导出五表：advisory_orders、action_mappings、position_dispositions、funding_conditions、publication_checks。每个原动作恰一映射，每个原成员与登记持仓均有处置；未知/排除保留，不把就绪子集当全集。

申购指令是用户实际支付金额，赎回指令是份额，执行方向known且>0，另一方向not_applicable。金额、份额与源指令精确相等；本链不重新优化、不重新选择批次、不在发布时舍入。批次分配沿冻结AllocationDimension/状态记录及来源合同唯一重建并核份额守恒；证据不足回SC07。费用、参考份额与预计净到账使用Estimate，绑定真实来源、合同、假设和时点；参考净值不是成交净值，未到账应收不是当前可用资金。

发布检查包括账户记录/资金/用户约束、真实合同/费阶、申赎状态和提交窗口、资格与必要边界。valid_until取全部必要可证有限边界最早值并保留源端点；未知必要界不填TTL，实际新事实触发的失效条件单列保留。过期只作历史材料。published_at未发生时为unknown，SC09实际复核时间单列；SC10记录真实交付，不回写本链不可变对象。

no_action仍需完整比较、风险与发布依据，不出零金额订单。observation_only不出任何交易指令，保留原动作、全集处置与Gap。risk_recovery只沿用单独真实政策授权的冻结动作；恢复消费闭环需当前上游接口与独立资格证据，不能用普通许可冒充。critical规则通过才可报告，高建议用途另核全部适用规则；producer不能自签许可。

SC01只读准确Context及investment-run三表，不直接读数据库latest。登记确认决定持仓，卖出到账不再扣份额；资金计划独立。涉及到账或偏好变化，登记/更新真实事实并重算受影响方案，不发布假定条件已成的订单。首次加两次失败停止后链。本阶段静态契约检查不代表业务、模型或科学资格通过。

本链SC05读取仅限准确当前市场快照及原动作所需contract_vintages/真实合同原文；不消费DataView/current_view、panel、labels、independent_view，不借GridRule子引用旁路SC05 whitelist。
