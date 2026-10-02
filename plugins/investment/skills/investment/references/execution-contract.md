# 强制源码、执行和自动验证契约

公开schema4，插件0.0.1；宿主沿用有效用户确认，数据和模型结果须保留来源/时间/版本/实际核验范围。内部工具存在不等于实际caller已执行。

investment.py在请求业务校验前统一加载锁文件依赖并检查Asia/Shanghai；不依赖外部启动包装。非数值daily报告的首次决策时间与request_hash封存为decision_clock，同请求恢复复用，原记录及预算保持。精确来源时间按真实偏移转换为北京时间，日期精度保留；转换仅影响显示，原始证据不变。

| 生命周期 | 必须完成 | 合格边界 |
|---|---|---|
| 金融事实 | 原用户本金/风险/账户与金融身份、真实成交/现金流/权益/到账 | 未确认钱不算现金；缺估值保留事实与pending |
| 来源 | 登记免费官网原文/真实目录、raw字节与发布/取得/修订 | lead不作事实，覆盖只声明实际有限集合 |
| 经济内容 | 原文数值/单位/期间/动作、stable sector/event/source revision | 重审不刷新first known，未知指标/预期不编值 |
| 发现与质量 | 同主体身份/同类组、逐产品费用、模型依赖质量和gross边界 | source身份完整不代表统计就绪或全部尽调 |
| 资格家庭 | H风险全集、Q合格买入、真实共同面板/clock/fit与预算 | 新C不影响合格AB，未知held不删除 |
| 预测 | Mature/PIT行业与多日期EN、训练内词表/字典、完整现金权益 | 时间/跨资产误差配对，当前fit排除cal labels |
| 政策 | declared、有效[start,end)、复核次数/历史预算；冻结有限source精度动作和非预知未来现金规则 | 过期/示例/额度不足保留研究与风险，无当前订单；end不是持仓到期 |
| 校准 | 原独立OOS路径对/误差/本金损失、联合块maxT/MC秩精度 | 固定信度/窗口/seed，条件诊断不升格市场证明 |
| 发布 | 重新来源/数学/账本/报告核验，租约/CAS/hash/截止期 | 只有真实当前资金/零追加支持当前订单 |
| 反馈 | 原金融事实追加及实际差异对账、正式试验完整样本 | 不能修改原文/账户或挑盈利观察过关 |

## 阶段与实例

每个适用业务/数字stage producer后执行validator，包括新质量、source边界、readiness/family、numeric_paths/numeric_mpc和当前compile；全部结果再operation与最终复核。每个实例名以受信base和源context/family哈希绑定，不接caller新validator；manifest保存原输入、原结果、验证/失败、接受指针和真实重复次数。

StageRuntime每stage/instance每round最多3次含首次；恢复不重置。依赖leaf耗尽立即停止外围，不再叠3次。最终最多3轮，下一轮重做派生产物，但原金融身份/输入含义/阈值/seed/错误预算保持。LeaseLost立即退出，旧generation不能发布。

partial列scope/checks/required_actions且非trade_ready，不能自称passed来绕过。源格式/模型能力确实不足是合法限制；源被篡改或计算关系不一致则失败并真实重做源码负责producer。

## 独立核验与事务

原文/捕获/身份/费用重新读取raw bytes与locators；经理全集独立从完整主体原文及续表重建，真实覆盖声明与端点支持证明，未知布局保留unknown仅关闭Q-On；模型输入/Scaler/系数/各inner成熟版本、完整NAV/现金权益目标和资金/份额/风险journal从数学关系核对，成功重跑只是另外一个重现证据。known dividends/expected payout与today available不能混同，source透明不保证未来报价不变。

fee_inspect的document_formats_v2先核原产品、发行人及TT页面主体，再解析优惠费率、开放状态、金额/持有日区间、业务时钟和精度。金额的<、≤及整数持有日端点分别保留；不凭“通常15:00/T+1/T+7”填值。原registry快照随捕获存为不可变ref，迁移只改物理ref，不更新raw字节、语义document_id或quote_observed_at。每个行动保留适用缺口；原文不明的部分赎回阈值不冒充0，普通现金计划不把养老金锁定款计作可用现金。

quote_exit_details和quote_exit_at_age_details返回gross、contractual_fee、rounding_loss、net、effective_cost。净额截位保留原份额×NAV毛额；毛额四舍五入按原规则另算。独立cash_reference用自己的Decimal区间、精度和日历关系复算，不能调用生产报价作为真值。模型price_sell日志的fee是effective_cost，fee_scope=source_estimated_execution_cost，contractual_fee、rounding_loss和receivable分别核验；实际反馈fee仍是原报告费用，实际gross/net/date原样入账并对偏差留痕。终点财富使用来源net；原毛额与舍入毛额的调整可能有符号，不能将它强制当非负损耗。`path_execution_cost+terminal_exit_assumption_cost=fees`，路径执行与终点假设估值分别扣一次，不能再从已经净计的`W_H`重复减fees；终点退出假设不是当前订单。实际窗口费加全计划最坏情景fees继续保守预留滚动额度。

sell_fill可携带实际证据actual_redemption_confirmed_at；确认不得早于price_date或晚于known_at/recorded_at。确认日结束的持有费期只用该明确终点，执行日结束则用实际price_date，不把成交经济时间按截止钟重新申报，不将晚到信息时间猜作确认时间。缺明确确认终点保留报告费用/净额，记录expected_quote_unknown及补核任务，不借冻结rate填预计费用。该字段参与原事件内容及自然成交身份核验；原记录不会被后台补写。模拟确认按来源定价/确认规则生成，日期只在各自订单中使用。

正式pipeline先`trade-history`重建真实费用/动作与历史预算，再`readiness`核数据和实际候选资格，`trade-inputs`核declared、有效窗口和复核上限，并在active正式统计结果之前用稳定request_id原子预留复核记录。并发不能占同一剩余额度；预留成功后同ID恢复只计一次，无赢家、失败或中断仍保留已消费记录。validator仅调用只读read_inputs重建，不产生业务预算写入；发布前再次核有效许可，族end_at进入最终提交截止围栏。历史使用记录不完整时保持unknown，不自动reset为零。每步验证及最终三轮上限与这项统计预算分别核验，重跑不能再分配新的误差额度。

昂贵计算/报告重建在写事务外，租约heartbeat可续；最终事务比对状态snapshot、owner/generation、源码/refs/core hash、新闻/风险/条款有效期和expected版本后原子提交。automatic_validation绑定原manifest/final_review/operation_bindings/core hash及round。不修改金融事实或生成的报告来匹配校验。

## 宿主与统计边界

宿主必须阅读语境、描述机制/反证、源不足和已定价判断，不将语言模型推断当原文事实或收益概率。真实source adapter须成功/失败实样本；不编造标准网页/历史时间来让接口通过。

比较限定已核源/统计/可执行政策与声明计算范围，不声称全市场最优、完全经理能力或未来风险保障。工程手算/性质/故障与明确合成场景、真实HTTP与未成熟/前瞻效果分开。每个通过对应实际执行/selector/exit证据；源错误修生成/解析/校验源码，不patch正式cache的数据或放宽预期。

全部金融、源、研究、失败按[存储](storage.md)分卷。旧已完成ID只返回原scope/report，新时点/信息/风险/账户用新ID。重复同数据不增加独立市场样本；未来outcomes pending。
