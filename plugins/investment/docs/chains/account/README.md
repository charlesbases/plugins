# SC01：基金买卖交易记录

文档版本：v1。状态：已有确定性交易登记及独立验证；当前接收强格式 JSON，自然语言转强格式的宿主适配尚未实现。

[项目入口](../../../README.md) · [总体架构](../../architecture.md) · [公共契约](../../contracts.md) · [内部说明](internal.md)

子链标识：SC01。说明目录：docs/chains/account/。代码包：[src/investment/chains/account](../../../src/investment/chains/account/)。精确规范：[SC01 v1](../../spec/v1/sc01.md)。

## 职责与模型

根据用户描述登记基金买卖，读取上次实际SQLite记录，处理本次交易、明确到账答复及预计时间，输出本轮冻结的累计交易、登记份额和待完成交易。不维护余额、本金、资金来源或账户净资产。分析可用金额由独立FundingPlan提供。

交易单位是一只基金的一笔买或卖，以trade_id稳定识别；同基金不同交易独立。一笔交易只有一个确认状态和一个到账状态，整笔完成或整笔未完成。confirmed_shares只在confirmation=confirmed时贡献持仓；卖出到账只完成款项阶段，不再次扣份额。

确定性模型为：某基金登记份额 = 已确认买入份额之和 − 已确认卖出份额之和。使用精确十进制，不用金额/净值反推份额。相关确认份额未知时，结果为unknown。登记范围固定为已确认零持仓起点以来的recorded_transactions_since_start，不宣称包含未报告的分红再投资、折算或转入转出，也不宣称等于券商可赎回份额、批次成本或完整账户资产。

模型依据是状态机、交易事实归并和份额守恒；expected_due是用户约定的记账策略，不能视为平台已经实际到账的证据。没有预测模型或市场收益承诺。

## 强格式输入

外壳沿用StageInput，stage_id固定SC01，精确保留三个角色：

| role | 类型 | 必须携带的内容 |
|---|---|---|
| transaction_facts_ref | ArtifactRef<TransactionFacts> | source_record_ref及base_generation |
| changes_ref | DatasetRef<Changes> | 本轮Mutation，与SourceRecord.changes逐字节规范投影一致 |
| task_scope_ref | ArtifactRef<TaskScope> | 账户、initialize/refresh、用户事实时点及请求用途 |

SourceRecord字段为record_id、source_type、operation、account_id、base_generation、confirmed_at、transaction_as_of、platform、currency、opening、changes、arrival_replies。首次initialize要求该账户无记录、base_generation=0、交易变化和回复均为空，确认零持仓起点。之后refresh读取当前真实账本generation，只提交变化，不重新提供整个账户。

每笔Trade的全部字段和封闭格式见[字段表](../../spec/v1/fields.md)、[Schema](../../spec/v1/sc01.schema.json)。字段用途如下：

| 字段 | 定义及下游用途 |
|---|---|
| trade_id、instrument_id、side | 交易幂等、精确基金/份额类识别、buy或sell |
| amount | 买入实际含费总支出；卖出整笔净应收/实收。只接受实际值，估计保留unknown |
| confirmed_shares | 实际确认份额，用于持仓推导；到期不能填入申请份额 |
| requested_shares | 实际提交且未确认卖出的申请份额，用于待确认赎回约束；买入为NA |
| fee_amount | 实际手续费，未报告为unknown，不默认为0；下游成本/费用分析只计一次 |
| trade_at、holding_start | 实际交易时间、明确持有起点；卖出holding_start为NA。用于时序及适用持有期核验 |
| expected_at | 唯一整笔完成期限；买入指份额确认，卖出指款项到账 |
| confirmation | state、actual_at、basis；份额确认阶段 |
| settlement | state、actual_at、basis；款项到账阶段，买入固定NA |
| last_arrival_reply | 系统维护的最近一次明确完成/未完成回复，无回复不能覆盖 |

Mutation只有create/update/correct。create必须新ID；update/correct绑定旧行SHA；update仅补未知、推进合法阶段或更新预测，已知实际值冲突必须correct加明确原因。补手续费、金额等不自动升级到账依据。所有字段必填，unknown/NA采用结构化Optional对象，不让AI补零或解释自由文本来决定控制分支。

## 字段必要性与消费者

| 字段组 | 实际消费者/作用 |
|---|---|
| 基金、买卖方向、稳定trade_id | SC01去重及确认份额归并；SC05/07/09识别基金合同 |
| amount、confirmed_shares、requested_shares | 实际交易规模、登记持仓、已提交未确认赎回约束 |
| fee_amount、holding_start、真实trade/phase.actual_at | 下游成本、适用持有期、事实时序；未知只限制依赖它的计算 |
| expected_at、两阶段state/basis、last_arrival_reply | 下次SC01处理预计完成/答复，区分实际与推定，避免重复扣份额 |
| meta范围/来源/时点/generation/digest | 下一轮真实基线、来源隔离、CAS和事实时间边界 |
| last_commit | 提交已成功但产物未收尾时恢复；每账户只保当前一份 |

买入amount为实际含费总支出；卖出为整笔净应收/实收。估计不能写成known实际，已知实际值冲突必须correct。交易/阶段/回复所有业务字段必填；last_arrival_reply由系统维护。没有余额、流水资金来源、分批数组、尾款或永久system accounted_at。

## 预计完成处理

只读--prepare根据上轮仍未完成且未到期的交易返回可选问题，不写库、不占attempt、不持有问询期间锁。宿主整理用户答复为同一正式ArrivalReply格式，再执行正式链路。

| 条件 | 确定性处理 |
|---|---|
| completed | 整笔完成，阶段依据user_reported；未给出的实际时间、金额、份额仍未知 |
| not_completed | 本轮禁止按期完成；仅撤回仍属expected_due的判断，保留用户实际确认的份额/到账事实 |
| 新预计known | 必须晚于报告时间，替换旧预计 |
| 新预计unknown | 撤销预计 |
| 新预计NA | 未提供；旧预计仍未来可保留，已过期则撤为unknown |
| no_reply | 不改变最近明确回复，也不恢复已撤销的旧预计 |
| 无否认且到期 | 整笔阶段按expected_due处理，不制造实际数字或时间 |

日期按Asia/Shanghai当天00:00判断；精确时间按绝对时点。截止时点只取本次invocation.created_at，三次尝试不变，decision_at不能使交易提前到账。买卖各阶段依据分别保存；卖出实际份额已确认但现金仅按期推定时，否认到账只恢复整笔待到账。

## 外部输出与交接

TransactionContext包含account_id、generation、ledger_digest、transaction_as_of、known_as_of、execution_cutoff_at、platform、currency、opening、source_authority、trade_count、trades_ref、positions_ref、pending_transactions_ref、missing_facts。所有派生表和Context是本轮准确的investment-run引用。

持仓表含基金、登记确认份额、贡献交易ID和范围。pending仅含trade_id、instrument_id、side、pending_phase、expected_at；完成后从该表移出，原交易行继续保留。没有累计到账、剩余尾款或正式业务Reconciliation对象。

SQLite只存trades和ledger_meta，详见[数据库规范](../../spec/v1/sc01.md#必要数据存储)。Context、持仓、pending、源输入、基线、候选、凭证和封存结果只在runs。真实用户事实时间不会因自动执行刷新；每轮控制提交marker可变化，只有业务事实变化才增加generation。

正式链路在本轮事实和必要历史记录上生成候选，经独立验证后提交并封存交接。失败最多重试两次，共三次；全部失败终止后链。提交已成功但产物未收尾时先恢复，禁止按普通失败重复记账。具体编排与恢复见 [内部说明](internal.md#提交恢复与清理)，精确关系见 [SC01 规范](../../spec/v1/sc01.md)。

允许用途由实际ValidationReceipt给出：确认份额缺失限制相应持仓用途，手续费或持有时间缺失只限制依赖它的分析；fixture始终observation_only。next_chain_allowed表示SC01控制成功，下游仍须检查allowed_uses和缺失事实，不能因此取得最终投资建议资格。资金计划与约束由后续任务独立输入。

## 验收与失败边界

流程与验证分别读取本轮输入依据，验证器独立核对份额守恒、整笔状态、时间依据及派生结果；完整规则和结构化反馈见 [SC01 规范](../../spec/v1/sc01.md)。单测报告和运行索引不属于 TransactionContext，不作为正式后链输入。模拟来源保留 synthetic_fixture 与 observation_only 限制。

活跃买入的holding_start未报告时须unknown，禁止NA绕过缺口；预计时间与实际trade_at若按区间能够确定先后矛盾则拒绝。同日日期精度区间重叠保持合法，实际时间未知时不推断精确先后。

运行命令、可选准备问询、实际场景单测和清理操作见 [内部说明](internal.md#运行与验证)；统一目录及每日说明见 [总体架构](../../architecture.md#runtime-data)。
