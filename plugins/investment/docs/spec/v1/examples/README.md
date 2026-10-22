# 子链输入输出示例

当前按单子链开发：使用上一子链已经完成并记录的完整输出，按本链输入定义准备输入，只执行当前链流程脚本和验证脚本。不得换成某个来源的专项试运行或部分文档，也不因示例复用而重跑已完成上游。

本页登记的正式交接示例是当前单链开发的唯一上游来源。执行前按 record.json 核对原运行、输出引用及摘要、时间窗口、来源和文档范围，再核对当前输入绑定的是该完整输出；记录缺失时先补齐，不从历史目录另挑一个合法样例替代。来源数和文档数由登记记录取得，不写成固定业务规则。

报告结论时注明本次输入、实际范围及本轮验证状态，局部通过不得代表完整子链通过。上述要求约束执行与交接；当前 SC03 单测入口仍接收 --input-example，尚未自动将参数与本页登记比对，不能声称已具有该自动拦截能力。

## 当前正式交接示例

SC02 当前交接示例来自时间筛选修复后的 `sc02-window-repair-df7121a8c5744a4794365190e4073db8`：**配置17来源，9个来源取得24篇材料，保留17条覆盖记录及全部失败/待补/用途限制。**窗口为2026-10-20T02:21:43.322670Z至2026-10-27T02:21:43.322670Z；9篇确认within、15篇uncertain。N07 passed，N10仍insufficient_evidence，凭证partial/observation_only，不代表整窗新闻齐全。

[SC02 完整输入输出文档示例（含原始 JSON、17来源统计及出处）](sc02.md)

| 子链 | 输入示例 | 输出示例 | 来源/状态 |
|---|---|---|---|
| SC01 | [StageInput](C:/Users/sun/.investment/test/sc02-live-all-6e623708b4be4b1695e7f354309dd5af/examples/sc01/input.json) | [TransactionContext](C:/Users/sun/.investment/test/sc02-live-all-6e623708b4be4b1695e7f354309dd5af/examples/sc01/output.json) | [原执行记录](C:/Users/sun/.investment/test/sc02-live-all-6e623708b4be4b1695e7f354309dd5af/examples/sc01/record.json)；原synthetic_fixture及observation_only限制保留，不是用户真实账本 |
| SC02 | [StageInput](C:/Users/sun/.investment/test/sc02-window-repair-df7121a8c5744a4794365190e4073db8/examples/sc02/input.json) | [完整配置EvidenceCorpus](C:/Users/sun/.investment/test/sc02-window-repair-df7121a8c5744a4794365190e4073db8/examples/sc02/output.json) | [实际执行记录](C:/Users/sun/.investment/test/sc02-window-repair-df7121a8c5744a4794365190e4073db8/examples/sc02/record.json)；17配置/9有材料来源/24文档，全部覆盖/失败/待补保留；SC01未重跑 |
| SC03 | [绑定修复后SC02输出的StageInput模板](C:/Users/sun/.investment/test/sc02-window-repair-df7121a8c5744a4794365190e4073db8/examples/sc03/input.json) | 单次执行已返回失败；无已验收的有效交接输出 | [输入模板出处](C:/Users/sun/.investment/test/sc02-window-repair-df7121a8c5744a4794365190e4073db8/examples/sc03/record.json)；[停止时原控制记录](C:/Users/sun/.investment/test/sc03-3f4e2353b9d140f19096ca1fe5bd06c1/sc03-verification.json) |

SC03输入的evidence_corpus_ref必须指向上述完整SC02输出的原引用和摘要。研究问题按SC03既有职责覆盖完整输入范围，不能改成仅核某一个机构或某一个指标来代替本轮完整材料。当前模板含事实/事件、来源关系、条件机制和全部缺口处置问题。

历史SC03运行 `sc03-3708728e48a8402b87bce128ff39d4ab` 已按用户要求停止。所用SC02完整24篇和17来源限制及corpus摘要 `9c63642ed80ccdf1827c28efc8a87d13460eb0086bd6e23264958e80488b9aa5` 保留；此前人工分批执行及提示干预不符合一次调用要求，不能登记成功输出。旧运行 `sc03-90b4241aeb4142989716f5d8244dfc02` 三次失败和[报告](C:/Users/sun/.investment/test/sc03-fd31f5a256754f74afc2c49df9abdfc0/verification-report.json)不变。模板准备、等待、分片草稿及局部检查均不替代完整合规验收。

完整输入调用 `sc03-de03056ed1a3488693f1c9dab9ffe7ea` 已三次失败终止，最近一次被A10确认相关声明遗漏，见[实际单链报告](C:/Users/sun/.investment/test/sc03-63c2e61a5f3f4dcdb8aa85cf2e7b1915/verification-report.json)。已有完整候选不代表有效输出，不能登记为成功示例或交给下一子链；修正后的新调用须另记实际结果，不覆盖此失败记录。

修正SC03提示后曾启动 `sc03-622a95df1d5e450097cdef366fe5091e`，其[旧执行状态](C:/Users/sun/.investment/test/sc03-a1888e35af264e7e878e4c1576c44cf4/sc03-verification.json)保留。查明所用旧SC02语料存在窗外材料后，已停止继续生成，不能将其等待状态当作当前续行入口。后续SC03应使用上表新模板建立调用，本次SC02修复未重跑SC03。

示例引用的原文、数据表及验收记录保留在原批次runs目录。单链单测必须一次调用，用已登记SC02输出和SC03输入模板自动执行流程脚本及验证脚本，根据最终输出判断成功或失败；失败后定位问题、修改流程脚本，再完整运行。不得按HostRequest逐批代做模型任务、追加提示或用多次resume拼接执行。scripts/verify_sc03.py现已实际一次调用返回失败，见[本轮报告](C:/Users/sun/.investment/test/sc03-5779c866bf514fec9697ebd9612a2c91/verification-report.json)；尚无本次实现的业务验收通过结果，不登记失败候选为交接示例。本页不提供人工续行命令。

## 先前误选记录

此前登记的BEA完整稿和摘要仅两份文档，属于专项试运行子集，**撤出当前SC02交接示例**。其SC03通过结果仅属于该子集，不能代表完整配置范围的输入已处理或验证通过。原运行记录保留用于复核，不删除、不冒称全量结果。

原 `sc02-live-all-6e623708b4be4b1695e7f354309dd5af` 的35文档示例因缺时区而保留7篇日期已能证明窗外的材料，**撤出正式交接**；原文件/引用/封存保留供缺陷复核。固定旧窗口的原始捕获回放得到28篇，仅是修复校准；上表24篇来自新窗口的实际全来源请求，不是从旧结果手选成功子集。当前输出摘要为 `9c63642ed80ccdf1827c28efc8a87d13460eb0086bd6e23264958e80488b9aa5`，下游必须绑定新记录。

## 各链输入输出登记

每链完成时保存一份实际输入与输出并登记；未取得的输出不伪造。下一链按角色读取上一链输出，本链额外协议和参数按自身定义准备，不把上一链StageInput整体作为下一链输入。

| 子链 | 已定义输入输出 | 当前示例状态 |
|---|---|---|
| SC01 | [交易记录](../../../chains/account/README.md) | 当前上游批次实际输入输出已保存 |
| SC02 | [EvidenceCorpus](../../../chains/news_collection/README.md) | 修复后完整配置示例已登记，partial限制见sc02.md |
| SC03 | [ResearchAssessment](../../../chains/news_assessment/README.md) | 输入模板已登记；历史失败及停止保留，暂无一次调用、无中途干预的已验收输出 |
| SC04 | [InstrumentUniverse](../../../chains/fund_universe/README.md) | 待登记已核实输入输出 |
| SC05 | [DatasetBundle](../../../chains/research_dataset/README.md) | 待登记已核实输入输出 |
| SC06 | [ForecastBundle](../../../chains/forecast/README.md) | 待登记已核实输入输出 |
| SC07 | [FrozenPolicy](../../../chains/portfolio_selection/README.md) | 待登记已核实输入输出 |
| SC08 | [QualificationVerdict](../../../chains/qualification/README.md) | 待登记已核实输入输出 |
| SC09 | [RecommendationSet](../../../chains/recommendation/README.md) | 待登记已核实输入输出 |
| SC10 | [ReportBundle](../../../chains/reporting/README.md) | 待登记已核实输入输出 |

原目录的SC01字段片段只作格式说明，不因存在就成为单测场景；当前单测使用本表登记的上游完整输出与当前链输入模板，不遍历未准入片段。
