# SC03：事实评审与经济假说

文档版本：v1。状态：外部业务契约设计，待实现与验收。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md)

本页定义本子链对外解决的问题、输入输出、科学方法和验收。内部步骤、适配器和分析器由子链自行编排，不能改变这里的字段含义、数值口径或失败语义。内部实现只生成 CandidateOutput，由独立阶段验证者签发 ValidationReceipt，调度者形成 SealedOutput。

## 外部业务定义

**问题。** 判断原文实际支持什么，并将事实转换成可检验的经济机制，而非直接转换为买卖信号。

**输入。** SC02 的 EvidenceCorpus、assessment_protocol_ref、analyst_identity_ref；账户风险对象可以用于提出待核问题。

**输出 ResearchAssessment。**

| 字段 | 类型与语义 |
|---|---|
| facts_ref | fact_id、assertion、document_version_ids、quote_locators、event_time、known_time、verification_status、support_refs、conflict_refs |
| events_ref | 同一事件的归并身份、时间、主体与事实关系 |
| hypotheses_ref | hypothesis_id、supporting_fact_ids、mechanism、affected_exposures、expected_direction、horizon、alternative_explanations、falsification_conditions |
| economic_observations_ref | 指标、原值、单位、币种、经济期间、源版本与可得时间 |
| counterevidence_ref、dispositions_ref | 反证、每个研究问题的处理状态与理由 |
| unresolved_questions | Gap 数组，保留重大未知 |
| analyst_identity_ref、assessment_protocol_ref | 方法版本、分析者/模型身份与解释生成时点 |

verification_status 为 source_reported、cross_checked、disputed 或 unknown。cross_checked 必须满足事前定义的证据规则，转载或同一来源的重复发布不自动算独立佐证。

**方法。** 证据关系图和可证伪经济机制分析。行业影响可以从需求、供给、成本、政策、现金流等渠道提出；因果结论若需要识别条件，应单独说明并提供相应实证，叙事本身不完成因果识别。

**内部链路。** 全部待评审材料 → 声明/引文抽取 → 支持与冲突核对 → 事件去重 → 行业机制及替代解释 → 经济观测与未决问题。

**验收。** 引文绑定 SC02 的准确版本；事实、假说和预测分列；支持关系不循环，不把后续模型结果倒填为原始事实；行业方向和期限保持假说属性，不输出未经验证的收益概率。

**失败路径。** 引文错误拒绝对应声明；冲突事实保留 disputed；机制证据不足只进入探索性研究。

**历史解释边界。** 记录 LLM 身份、版本、解释时点及可核训练时间信息。禁止检索未来信息不能排除模型训练知识泄漏。无法排除污染的历史解释标为探索性，匿名化只能作为敏感性检查。[Glasserman 与 Lin：LLM 金融回测前视偏差](https://arxiv.org/abs/2309.17322)
