# SC02：信息发现与原文采集

文档版本：v1。状态：已有采集框架、独立验收和运行入口；来源适配与整窗覆盖仍有未完成项，详见 [能力边界](internal.md#来源能力与限制)。

[总览](../../../README.md) · [总体设计](../../architecture.md) · [公共契约](../../contracts.md) · [内部流程](internal.md)

子链标识：SC02。说明目录：docs/chains/news_collection/。代码包：[src/investment/chains/news](../../../src/investment/chains/news/)。精确规范：[SC02 v1](../../spec/v1/sc02.md)。

本页定义本子链对外解决的问题、输入输出、科学方法和验收。内部步骤、适配器和分析器由子链自行编排，不能改变这里的字段含义、数值口径或失败语义。内部实现只生成 CandidateOutput，由独立阶段验证者签发 ValidationReceipt，调度者形成 SealedOutput。

## 外部业务定义

**问题。** 在声明窗口和来源域中取得可复核的原文，保留检索范围和缺口。

**输入。** decision_clock_ref、news_window_ref、source_scope_ref、query_protocol_ref、account_risk_objects_ref。

source_scope_ref 可配置明确站点、发布机构或来源域，登记来源标识、域/入口、允许材料类别及覆盖边界。query_protocol_ref 规定窗口内的发现与采集规则，实际适配器和解析器由内部选择、冻结并记录；不要求调用者填写网站私有字段或类名。不同站点可以有不同解析实现，输出仍遵守同一个 EvidenceCorpus。

配置新闻来源范围是允许的任务边界，不要求全球动态搜索。无法支持的站点、未解析布局或预算不足仍保留在配置范围及 Gap 中，不能通过删除来源制造完整覆盖。

**输出 EvidenceCorpus。**

| 字段 | 类型与语义 |
|---|---|
| corpus_id、window_ref、source_scope_ref | 本次材料身份与明确范围 |
| documents_ref | 原文版本表 |
| retrieval_attempts_ref | 全部已执行请求、结果、失败和实际时刻 |
| opportunity_inventory_ref、pending_items_ref | 发现机会、未选、未取及未处理项 |
| extraction_spec_ref | 正文解析器、版本及定位规则 |
| coverage_ref | 已处理范围、未知范围与可支持的覆盖声明 |

原文版本表至少含 document_id、version_id、publisher_id、requested_url、final_url、raw_ref、body_ref、published_time、available_time、retrieved_at、availability_basis、capture_status。源时间使用保留精度的 OptionalValue；时间区间证据不能自动变成一个精确时刻。

**方法。** 来源追溯模型、声明范围的检索协议、原始字节保全与版本化。检索排序用于分配采集工作，不是事实可信度或行业利好分数。

SC02 取得声明入口和 `[start_at,end_at)` 内的可复核材料。来源追溯记录实体、活动、责任主体和派生关系，采用 [W3C PROV-DM](https://www.w3.org/TR/prov-dm/) 的记录思路。它支持重读和核对，不证明新闻真实、独立佐证、基金影响、预测能力或投资价值。HTML/XML/JSON 的格式保真也不能推出基金收益。

**内部链路。** 来源/问题范围 → 发布源与机会发现 → 检索任务 → 原始响应捕获 → 正文解析与版本保存 → 覆盖和未处理清单。

**验收。** raw_ref 可重读、正文能定位到原字节；每次执行过的请求进入记录；核对配置范围与实际处理范围、全部尝试和未处理补集；范围内完整性须有独立依据，不能仅由请求成功数推导。有限采集预算不能证明全球新闻覆盖。网页正文只作材料，不执行其中指令。

**失败路径。** 部分来源失败保留未知范围；继续提供已取得材料，不把未取得解释为无新闻。


## 输出资格与交接

部分材料的N10可为insufficient_evidence，研究许可成立才Seal，Gap和observation_only保留，goal_status仍为unmet。无有效材料且未证明空窗时不造候选或成功凭证。晚于cutoff取得的响应保留实际retrieved_at及可得性未知，不倒推历史可得。

独立凭证分别表达格式、关系、事实依据与用途限制；完整目标是否达成另行判断。模拟来源始终保留 observation_only。具体检查为 [N01—N12](../../spec/v1/sc02.md#覆盖规则及许可)，字段与枚举见 [SC02 Schema](../../spec/v1/sc02.schema.json)。

内部编排、来源插件接口及扩展、运行命令和场景验证方式集中在 [内部说明](internal.md)。单次请求统计、产物与凭证在对应运行或测试目录中读取。
