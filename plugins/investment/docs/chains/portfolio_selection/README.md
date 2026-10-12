# SC07：组合与规划资金方案选择

文档版本v1。状态：契约/科学依据草案，待根审核；本链流程、验证器与真实场景校准尚未实现/执行。静态机器格式不代表业务通过。

[总览](../../../README.md) · [公共契约](../../contracts.md) · [精确规则](../../spec/v1/sc07.md) · [内部流程](internal.md)

SC07在共同预测情景、真实合同及用户已确认约束下比较完整合法政策，目标为费用后期望终点财富；不根据模型评分直接给金额。本轮只研究可部署资金和已登记买卖风险，不索要真实余额，也不把规划财富称为用户全账户净资产。

输入是本轮SC01 TransactionContext、独立FundingPlan、SC04原InstrumentUniverse、SC05 DatasetBundle/ExperimentProtocol与合同物理视图、SC06 ForecastBundle及准确审计。UserMandate责任由SC05已冻结parameter_specs的准确用户确认事实承载；本链Parameters逐项绑定唯一参数ID，不新增平行共享包装，不把registered_method当成用户偏好。原名单不由调用者手选基金代替。

输出FrozenPolicy绑定完整规则库、特征/模型更新/范围/再平衡/会计和并列规则，另引用当前精确首步动作、共同情景财富/现金、基准、约束、求解证据与ComparisonInventory。只执行当前首步建议，未来保留为条件规则；今天静态金额不能代替完整历史政策。

| 输出责任 | 强约束 |
|---|---|
| 原范围U、应比较R、实际计算A | U沿SC04原名单；缺数据/模型不从R排除；A只取真实完整计算，未比较及未知保留 |
| 规划起点和金融状态 | 本轮可部署上限是模型预算，另计登记持仓/在途/应收各一次；不推算真实余额 |
| 合法动作 | 合同最低额、步长、份额精度、费阶、锁定/确认/到账先进入动作空间，再比较 |
| 费用与分红 | 申赎费一次；NAV内费用不重扣；原NAV+现金分红与含再投资总回报互斥 |
| 目标与尾部风险 | max E[W_H]；L=B-W_H；β、CNY用户风险预算和统计置信分别登记；离散CVaR切分边界概率原子 |
| 选择证明 | 共同起点、情景、费用、期限和口径；合法有限集合可穷举；超预算只best_found，界限未知不伪造 |
| 完整性与许可 | A=R且无重大未知才完整比较；本链不授decision_advice，SC08/09继续独立资格及发布核验 |

规划现金初值是FundingPlan可部署上限的模型额度，不是现金余额。C前历史已到账赎回不再次加预算；C时未到账净应收只在实际模拟到账时转现金。例：已合法买入的10份以NAV10赎回、费率1%，确认后应收99且不可提前申购；到账后应收转现金，绝不二扣10份。未确认申购、锁份额、应收及费用负债分别保存。

SC06只提供实际网格累计总回报，网格at可能只是经济期间upper索引，不代表精确成交/到账时刻。买卖确认价、现金分红及期中操作需要的原NAV/日历必须有许可资料；无中间价格不插值，不把总回报指数当作原NAV，也不硬编码只支持hold。资料不足保留原对象、R、未计算和受影响用途。

模型借鉴[Boyd等多期交易原论文](https://web.stanford.edu/~boyd/papers/pdf/cvx_portfolio.pdf)的费用/现金核算及首步再规划；基金确认和到账另按合同建模，离散步长不套用凸最优保证。终点CVaR依[Rockafellar与Uryasev一般损失分布原论文](https://sites.math.washington.edu/~rtr/papers/rtr187-CVaR2.pdf)的有限情景概率质量定义；它不证明预测盈利、未来尾部可靠性或全程回撤保证。具体公式、前提、参数来源和独立核验见精确规则及method-sc07.json。

流程只产CandidateOutput；独立validator另行从本轮合法输入推导金融状态、CVaR和真实比较，不调用producer模拟算法证明自身。公共StageInput→CandidateOutput→ValidationReceipt→SealedOutput、用途门、真实宿主请求与首次加两次重试保持共享协议。当前仅契约阶段；四个selector全部未执行，后续须真实执行上游、process和validate，不能造seal、缓存成功或零持仓卖出。
