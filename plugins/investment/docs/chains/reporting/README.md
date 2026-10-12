# SC10：报告交付

文档版本v1；状态：仅初始强契约/科学依据，validator、producer、正式prompt和四selector业务执行均未实现或执行。

[总览](../../../README.md) · [公共契约](../../contracts.md) · [内部流程](internal.md) · [精确契约](../../spec/v1/sc10.md) · [Schema](../../spec/v1/sc10.schema.json) · [运行方法](../../spec/v1/method-sc10.json)

SC10准确说明本轮已封存资料、完整原基金名单、真实比较、全部持仓在途风险、原建议及限制，记录真实本地交付。不重新研究、排名、优化、改金额或签发金融资格。LLM可组织中文，所有实质断言绑定原对象字段，事实/机制/估计/条件/未知分列。

主调度传实际已完成上游业务payload的准确引用、必要SC01账户、范围和本轮ReportingContext；共享控制器核本轮Seal/许可，SC10不重跑上游业务。输入最低reporting权限不等于decision_advice；原建议只按原许可强度呈现。账户/范围缺失或上游三次失败不能制造正常ReportBundle，根仍从前缀和控制记录形成pending/unmet结果。

ReportBundle完整字段和闭合定义见精确契约。它绑定实际中文Markdown、完整原JSON机器附表和索引、逐断言claims、覆盖/资格摘要、独立预核对及真实delivery记录。review_workbook本版不适用，不声称Excel；Markdown媒体真实text/markdown。U/C/E/K/R/A/未比较及全部positions/pending/mandatory逐项保留，原组别、身份、冻结分母和未知不删，不仅topN。源unknown不能改成空名单；源缺口仍可形成明确观察报告，不能升级成最优或合格无动作。

正文沿用SC09原decision_at/publication_checked_at/估计/批次/金额/费用/有效区间和事件条件，不把预测份额当成交。SC09 published_at仍unknown；SC10自身真实交付时刻独立记录，并核当时所有必要有效界、exclusive和已收到变化。过期/变更/必要未知按历史或观察表达，不回写immutable SC09、不新增默认TTL、无消息不证明条件永远不变。

严格顺序：实际报告字节及全附件→独立新上下文预核对→宿主真实本地读取/核hash及路径/登记接收→候选→独立最终验收/Seal→根证据与总结果。delivered表示宿主接收全文件，不代表用户阅读；只保存草稿不算交付。预核对只按真实targets×T01—T09/T15十规则核固定实际内容：T08用checked_at核原时点/条件，T09核实际字节/完整原附件/全部内容依据，不依赖未来接收或final review；失败不交付。最终仍核全16，T10只在最终核四步骤完成，T08按实际delivery.completed_at重核时效。delivered须全required文件准确实收，delivery_failed保留真实错误/失败项和准确已收子集；两状态均须独立通过相同完整内容门，无内容不能借失败免验；最终失败保留之前真实发生的交付，不能改历史掩盖。

仅从合法封存输入表达的来源/集合/时序模型，依据W3C PROV原规范；不新增收益模型或保证。完整T01—T16、两用途门、四个正式Host steps、原四selector可达准入均在method中登记；登记不等于实现或验证通过。后续validator和独立校准、根冻结后producer，均待根精确阶段派工。
