# SC06：预测与联合情景

文档版本 v1。状态：仅契约阶段，流程、独立验证与校准待实现；登记不表示金融资格。

[总览](../../../README.md) · [项目约束](../../../AGENTS.md) · [内部流程](internal.md) · [精确规范](../../spec/v1/sc06.md) · [Schema](../../spec/v1/sc06.schema.json) · [方法](../../spec/v1/method-sc06.json)

本链从获准历史成熟信息形成可重读条件预测和共同情景。输入为本轮真实封存的 SC05 DatasetBundle 和 SC04 InstrumentUniverse；协议与训练/选择/当前物理视图沿 Bundle 准确引用读取。不读取独立验证未来标签或祖先审计 raw。上游 reporting 仅提供缺口读权，高级训练/选择/情景用途另核实际许可。

ForecastBundle 保留原 U、发现/取材审计与全部实际风险，输出模型目录、规则、折内预处理、训练/调参审计、历史 OOS/当前预测、同步残差、共同情景、假设及诊断。每个原成员都有 ready/proven_not_applicable/unknown、准确依据与影响用途；缺数据不等于有据排除，instrument_ids 不替代 U。

特征直接来自SC05协议预先冻结的有序predictors，对每个实体保留适用列表P(e)的原顺序与全部未知槽；历史退出成员无需current行。来源定义只读取准确source_rule_ref本体，不继承raw权限，也不从下载成功列或今日成员反推特征全集。

首个模型族为实际 ElasticNet，比较基线为同训练标签的 training_mean。每个 asset/horizon 直接拟合 origin 到端点的累计算术总回报，identity变换；折内标准化、成熟标签前向MSE选参、完全并列按冻结顺序。保存系数/截距、训练成员、实际输入/版本、迭代与数值审计，验证器独立重算；未拟合不报告已训练。[官方目标与停止准则](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.ElasticNet.html)

情景以全部所需 asset×horizon 在同一历史 origin 的成熟 OOS 残差完整向量为单位，沿 origin 抽连续重叠块。每个向量是一条 scenario，全部资产共享 selected_origin、scenario_id 与权重1/draws，R_s=mu_current+e_selected。未来网格仅 C 和真实h端点，各R相对C累计，不串乘、不造中间价格。R<-1或零财富后恢复正财富使经济路径不可用；全部原场景、缺口与成员保留，不clamp或删坏路径归一化。

draws不是独立历史样本量，条件均值不等于完整分布。弱依赖、稳定性、矩及尾部证据独立审查；ESS/未见尾部支持保持未知。数值误差政策与独立校准要求见精确规范，数值通过不代表未来盈利。

诊断保留协议规定的ordered_origin_block_inventory及documented_assumption_review同名字段，适用证据最多conditional_support；本链无CNY财富loss时保留empirical_loss_tail_inventory明确依赖，交SC07/08处理，不用回报残差冒充货币损失。[已读ETH原报告的方法边界](https://stat.ethz.ch/Research-Reports/70.ps.gz)不构成本次金融场景的有效性证明。

流程只产生CandidateOutput；独立验证者签ValidationReceipt，控制器封存。reporting/evidence_review需关键身份、完整范围及真实执行检查；selection_comparison另需全部训练、数值、共同情景和适用证据门。本链不授予independent_evaluation/decision_advice；缺证据保留Gap，真实失败遵循首次加两次重试。

selection_comparison仅是供SC07研究比较使用的预测/情景输入许可，不是政策风险或CNY尾部资格。本链真实统计前提必须具备依据；后链CNY损失清单不足保留准确对象依赖，既不伪通过，也不机械撤销本来有效的预测输入。
