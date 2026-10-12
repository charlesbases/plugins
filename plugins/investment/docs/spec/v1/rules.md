# SC01 v1 确定性校验规则

机器格式以同目录四份 JSON Schema、registry.json 和 storage.sql 为准。JSON 对象封闭，禁止额外字段；UTF-8、排序键、无空白规范 JSON。禁止浮点数、重复键、NaN、负零和超出安全整数。金额/份额用十进制字符串，计算精度80且禁止舍入。unknown、not_applicable 必须用三字段 Optional 对象，不能用空字符串或0代替。

| 规则 | 必须满足的条件 |
|---|---|
| J01/J02/J03 | 规范字节、注册Schema、格式/小数合法；known金额及份额>0，known费用>=0 |
| R01/R02 | URI白名单、真实字节SHA/长度、表元数据、行数、主键完全一致。运行产物使用 investment-run URI；不提供历史 investment-data 快照定位 |
| I01 | 正式SC01输入恰有 transaction_facts_ref、changes_ref、task_scope_ref，scope与角色一致 |
| I02 | source与facts的base_generation等于本轮从实际SQLite冻结的基线generation；初始化为0 |
| I03 | source、changes、scope投影一致，身份/平台/CNY/零持仓起点/来源权威保持一致，fixture不可升格用户事实 |
| I04 | opening<=transaction_as_of<=confirmed_at<=information_cutoff_at<=实际execution_cutoff；scope时点一致 |
| I05 | 固定方法及4项参数完全匹配注册常量，重试不能改变方法或阈值 |
| I06 | 每笔一次mutation/reply；create新ID，update/correct带旧行SHA；correct须明确非空原因；known事实冲突不得普通update覆盖 |
| I07 | 判断时点只能取清单created_at，三次共享输入/基线/方法/时点。decision_at不驱动到账 |
| A01/A02 | 基线+mutation精确归并；同trade_id无重复账；基金、买卖方向不可变；无事件数组/部分状态 |
| A03/A04 | confirmation仅pending/confirmed/cancelled/failed；settlement仅not_applicable/pending/settled；买入settlement固定NA；卖出settled蕴含confirmed；取消失败不贡献持仓 |
| A05/A06 | 活跃交易confirmed_shares只能known或unknown，不能以NA逃避份额缺失；只有confirmed_shares是确认份额；requested_shares仅未确认卖出提交量，不能由时间转实际；确认前actual_at和确认份额不可known；卖出holding_start为NA |
| A07 | amount买入为实际含费总支出，卖出为整笔净应收/实收；估计不能写成known实际。known冲突需correct；金额不会按累计到账相减 |
| A08 | 实际trade/confirmation/holding/settlement时间须处于opening.start_at至transaction_as_of的已声明记录范围，并不得晚于用户报告；日期按区间拒绝确定越界，不能以日下界误拒同日记录。已知实际时序不得矛盾，预计不充当实际时间 |
| A09 | 确认买入减确认卖出=持仓；相关确认份额未知则持仓未知。已知持仓不得负，pending卖出请求量不能超过已知可支撑持仓 |
| A10 | pending仅列整笔未完成交易的confirmation或settlement阶段；到账不再次扣份额 |
| A11/A12 | context、三张派生表、缺失事实及来源时点必须由本轮事实精确导出；真实用户时点不因自动执行刷新 |
| A13 | 显式completed/not_completed优先；no_reply无报告时间/新预计且不覆盖最近明确回复。completed无新预计；新预计须晚于报告时点 |
| A14 | 否认本轮禁止自动完成；撤回过期预测。到期仅改变状态及expected_due依据，不填实际数字/时间；各阶段依据独立，补费用不得升级到账依据 |
| V02/U01 | 独立进程完整执行全部注册输出规则；fixture只能observation_only；缺失数值仅限制依赖该值的消费，不阻止有效记录 |
| D01/D02 | 两表STRICT、canonical行及meta都需Schema+语义+真实独立验收许可；拒绝未支持旧表布局，禁止清库/自动迁移 |
| D03/D04 | generation/digest CAS；交易与当前last_commit一事务。业务事实改变才增generation；控制marker每轮更新。未finalized禁止覆盖；先恢复绑定产物后finalize |
| S01 | 完成且无有效下游使用的runs可清理；未完成attempt/提交及有效下游引用阻止清理。清理不删交易；已清理旧产物不保证回放 |

路径启动约束使用T04：P=resolve(~/.investment)、T=P/test、R=resolve(--root)。正式模式必须R=P且source_type=user_confirmation；--fixture-mode必须R为T的严格后代且source_type=synthetic_fixture。T本身不能作为运行根。所有单测数据只能在~/.investment/test/下，包括独立data、runs、报告和辅助文件；不得使用AppData/系统临时根或正式data/runs。入口文件实际路径F在单测中必须位于R内，正式模式中不得位于T内；验证候选入口采用同一限制。解析实际路径，根/模式及入口命名空间校验在读取输入、建锁/目录和开库前执行；来源绑定在流程写候选前和数据库许可、提交、finalize时执行。非法根或跨命名空间入口启动拒绝退出2；业务阶段来源/根不匹配为T04/use_not_allowed，不放行后链。

到账策略：只有一个expected_at，买入指整笔份额确认，卖出指整笔款项到账。instant按绝对时点；date按Asia/Shanghai当天00:00，是执行策略而非银行实际到账证明。到期可能隐含完成未知确认阶段，但requested_shares仍不是实际份额。

not_completed：known新预计替换；unknown撤销；NA表示没提供，旧预计仍未来可保留、已经到期则撤为unknown。仅撤回依赖该预测的expected_due判断，用户已确认份额和实际到账不能无明确更正被删除。completed只证明整笔完成，不证明缺失的金额、份额、实际时间。

重复输入复用未清理invocation的相同冻结输入结果。稳定trade_id承担长期业务去重；没有永久source_record黑名单。同基金不同交易独立。恢复失败须明确“已提交，勿重复提交”，禁止重复落账。三次失败终止后续，逐次保留结构化失败规则、对象、字段和原因。

## SC01脚本职责与执行协议

一次正式尝试从production开始：process_sc01.py读取同一冻结输入、账本基线、方法与invocation.created_at，生成当前attempt候选；随后validate_sc01.py独立重读输入、基线和候选。最终ValidationReceipt必须精确覆盖J01/J02/J03/R01/R02/I01—I07/A01—A14/V02/U01全部28项，不以前置InputCheck代替输入依据校验。

流程只写运行候选，不写业务数据库、不封存、不放行后链。校验器验证实际用户记录、准确基线、份额守恒、整笔状态、预计依据、精确十进制、未知和引用，不能调用流程业务算法。流程与校验可共享Schema/引用/小数基础类型。校验通过后编排器才原子提交、封存、finalize，返回后续子链可消费的准确引用。

单测脚本只构造输入、调用这套流程/验证并独立判断结果；单测汇总invocations/checks/stdout/stderr不得加入CandidateOutput、TransactionContext或三张业务表，也不得充当后续正式子链输入。运行manifest的invocations另属控制索引。synthetic_fixture及observation_only保留为模拟来源及用途限制，不能删掉标记将单测结果升格为真实用户事实。

首轮无反馈；第二、三轮从清单解析上一attempt.failure_ref，验证实际SHA、Schema、相同run/invocation、前一attempt及failed状态。rule_id/reason_code/json_pointer为结构化反馈，reason仅解释；反馈不能替代用户事实。每次实际重新执行流程，不复用失败候选，冻结输入/基线/方法/截止时间不变。流程无候选时给出phase=production的强格式Failure；有候选则交独立校验。三次失败终止且next_chain_allowed=false。成功不要求input_check_ref为known。

已确认买入holding_start只能known或unknown；为保证待确认买入后续能正常转为确认，活跃买入均采用此约束。未报告不能用NA绕过缺口；取消/失败和卖出可按适用性使用NA。已知trade_at与expected_at若能根据时间区间确定预计早于实际交易，按A08拒绝；同日仅日期粒度且区间重叠不误拒，未知时间不猜测。

单测主体构造真实输入，实际运行流程和校验进程，再检查输出与数据库；科学预期来自用户事实及独立的份额计算，不来自生产者预期文件。校验器的正确/错误候选校准与故障注入是补充验证，不能作为新增基金业务场景。先校准并固定校验器，再实现流程。修改已固定校验器必须先证明漏洞或标准与需求矛盾，不能为迎合流程结果放宽规则。
