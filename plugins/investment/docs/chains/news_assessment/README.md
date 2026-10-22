# SC03：事实评审与经济假说

文档版本：v1。状态：业务契约与科学依据待审；验证器、流程与整链验收尚未完成。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md) · [精确规则](../../spec/v1/sc03.md) · [Schema](../../spec/v1/sc03.schema.json)

子链标识：SC03。代码责任为 src/investment/chains/news_assessment/；专用 schema 为 urn:investment:spec:v1:sc03。本链定义事实与研究假说，未取得事实依据的解释不能转成买卖信号。

**输入。** 本轮 SC02 已验收 EvidenceCorpus、assessment_protocol_ref、analyst_identity_ref。宿主从任务范围自动建立协议、研究问题和可核分析者身份，不要求用户手工传模型结果。可选账户风险仅用于提出问题，不能补造新闻事实。

**输出 ResearchAssessment。**

| 字段 | 语义 |
|---|---|
| facts_ref | fact_id、assertion、document_version_ids、quote_locators、event_time、known_time、verification_status、support_refs、conflict_refs |
| events_ref | 事件身份、主体、时间、事实关系和未决归并关系 |
| hypotheses_ref | 支持事实、逐步机制、实际暴露、方向与期限假说、替代解释、可观测证伪条件 |
| economic_observations_ref | 指标原值、单位、币种、经济期间、计量口径、发布版本、源版本和可得时间 |
| counterevidence_ref、dispositions_ref | 反证/修订/口径差异；每个研究问题的处理状态、理由和缺口 |
| unresolved_questions | Gap 数组，保留本链重大未知及上游真实覆盖限制 |
| analyst_identity_ref、assessment_protocol_ref | 分析者/模型身份、实际生成时点、协议与方法版本 |

verification_status 精确为 source_reported、cross_checked、disputed、unknown。source_reported 只表示原文报告此事。cross_checked 要求独立原始来源支持同一完整声明且没有未决冲突；转载、同源重复发布和模型多数票不能提升等级。所有字段类型、必填、未知/不适用、主键/引用、时间、单位与逐用途门槛见[机器规范](../../spec/v1/sc03.md)。

**方法。** 构建无循环支持图，把原文声明、经济观测和机制假说分列。机制须明确事实前提与条件假设，给出暴露、替代解释和证伪条件；不能由关键词套用固定行业方向，不输出未经验证的收益概率。无充分依据的机制保留 exploratory 和具体缺口，不能冒充因果识别。

**独立验收。** 确定性脚本检查准确源版本、引文偏移、十进制值、时间与引用图；独立宿主语义调用检查原子声明蕴含、冲突、事件身份、机制每步及全文/问题覆盖。语义输出为 SemanticReview，必须回到当前输入的原文，不能由生产者填“已验证”。生产者只生成 CandidateOutput；ValidationReceipt 与 SealedOutput 分别由独立验证者和调度者生成。

**输出资格。** 原文定位错误拒绝对应声明；同口径未决冲突保留 disputed；证据不足的机制保持探索属性。可授用途限于 evidence_review、hypothesis_research、reporting，各自按 A01—A17 门槛核定。观察结果不等于原始研究目标满足，更不授予预测、回测或建议资格。

运行类型与方法分别登记在 [registry-sc03.json](../../spec/v1/registry-sc03.json) 和 [method-sc03.json](../../spec/v1/method-sc03.json)。原文按冻结工作计划分批分析，跨片段/跨文档关系单独核对；全部文档及研究目标仍须覆盖。实际解释时间读取宿主事件，身份记录的生成时点不冒称模型解释完成。登记文件存在不代表提示词或运行链已实现。

**历史解释边界。** [Glasserman 与 Lin](https://arxiv.org/abs/2309.17322)讨论新闻情绪回测的训练期重叠与公司知识干扰；本链据此记录身份/训练时间证据，无法取得则 unknown。禁检索未来资料和匿名化均不能证明无训练污染。[FActScore](https://aclanthology.org/2023.emnlp-main.741/)仅支持逐原子声明对源核查的设计，其传记任务性能不能当作本链金融语义准确率。方法适用性、可达校准场景与限制详见精确规范。
