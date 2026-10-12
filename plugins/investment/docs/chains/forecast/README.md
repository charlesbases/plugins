# SC06：预测与联合情景

文档版本：v1。状态：外部业务契约设计，待实现与验收。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md)

本页定义本子链对外解决的问题、输入输出、科学方法和验收。内部步骤、适配器和分析器由子链自行编排，不能改变这里的字段含义、数值口径或失败语义。内部实现只生成 CandidateOutput，由独立阶段验证者签发 ValidationReceipt，调度者形成 SealedOutput。

## 外部业务定义

**问题。** 从当前信息及成熟历史中形成可验证的条件预测和共同不确定性表示。

**输入。** 获准的 SC05 数据、SC04 范围、ExperimentProtocol、target_definition、训练/选择分区，以及准确 discovery_audit_ref、source_acquisition_audit_ref；不读取独立验证的未来结果。输入保留 U 的完整发现及处置关系，实际训练视图按权限和时点资格提供，不将就绪子集改写为原范围。

**输出 ForecastBundle。**

| 字段 | 类型与语义 |
|---|---|
| forecast_id、information_cutoff_at | 本次预测身份与信息截止 |
| dataset_refs、instrument_ids、target_definition、horizon_ref | 准确数据、对象、目标及期限 |
| fitted_model_ref、model_rule_ref、preprocessing_ref | 模型产物、训练/更新规则及训练内处理 |
| training_audit_ref、tuning_audit_ref | 标签成熟、前向选参、样本与参数证据 |
| member_forecast_dispositions_ref | DatasetRef；U 各成员的 ready、proven_not_applicable 或 unknown、依据及影响用途 |
| historical_predictions_ref | 训练/选择流程产生的历史样本外预测，包含 origin、目标期和预测时输入版本 |
| current_joint_scenarios_ref | 当前共同情景、时间轴、资产路径与概率权重 |
| scenario_method_ref、assumptions_ref、diagnostics_ref | 分布构建方法、依赖假设和诊断 |
| missing_evidence | Gap 数组 |

**模型。** 首个候选基线可采用 Elastic Net：在训练样本上最小化预测误差与 L1/L2 正则项，用于相关协变量下的条件均值估计。具体目标、特征、惩罚参数及比较基线由协议登记，不能仅凭方法名称确认金融适用性。[Elastic Net 方法定义](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.ElasticNet.html)

条件均值不构成完整风险分布。联合情景需要另行登记方法，例如同步残差块重采样，保留基金间关联及时间依赖，并评审适用的弱依赖/稳定性条件、块长确定规则和尾部有效样本。方法假设不成立时不授予相关风险估计资格。

member_forecast_dispositions_ref 逐项绑定 member_id、entity_id: OptionalValue<ID>、原范围/来源审计、forecast_status、rule_ref、evidence_refs、reason、affected_uses。ready 表示本阶段必要输入和方法检查就绪，不授予 SC08 科学资格；proven_not_applicable 要有规则及证据；样本不足、缺字段或方法未运行保持 unknown。instrument_ids 只说明真实预测对象，其补集仍在处置表中，不能冒充完整同类比较。

**内部链路。** 原名单和逐项就绪处置 → 时间顺序训练折 → 折内处理与调参 → 样本外预测 → 冻结模型及更新规则 → 共同情景生成 → 概率与诊断验收。

**验收。** 逐成员处置完整覆盖 U，原范围、资料资格、真实模型产物及输出对象可互相追溯。未知成员和所有实际风险保留，下游比较是否完整须由原列表与真实计算另行核实。

标准化、特征选择和学习型插补仅在各训练折拟合。[数据泄漏规则](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage) 前向划分按标签覆盖及成熟时间隔离，不能随机打乱时间或把固定 gap 当充分证明。[TimeSeriesSplit 的时间顺序原则](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)

共同情景必须共享 scenario_id、time_grid 与 probability；概率有限、非负、总和为 1。原始权重的数值容差与归一化规则事先登记，归一化后的分布由全部比较方案共用，不靠任意重新加权获得通过。不得把各基金独立最有利路径拼成一个组合情景。输出说明预测估计与假设，不授予金额建议资格。

**失败路径。** 样本、尾部或方法证据不足时返回具体缺口；没有实际拟合不写成已训练，不制造默认上涨概率。
