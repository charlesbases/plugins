# 组合数学、调仓与追加资金

本规范是组合算法、资金方案排序和比较输出的单一来源。入口是安装版 `scripts/model.py allocate / allocation-validate / allocation-verify`。`allocation.py`、`allocation_statistics.py`、`allocation_runner.py` 是实际执行器；不依赖另写缓存脚本。旧 `validate-all/train/verify` 保留单基金条件研究基线，不认证新组合策略。

## 数学定义与证据边界

预测：Elastic Net以决策前已经成熟的总回报标签训练，目标为基金净值总回报，包含现金分红权益、净值内运作费用；投资者申赎费用另在组合层扣除。按固定日历窗口训练，在内部前向分割清除尚未成熟标签，标准化只拟合训练段。每日期、底层组合分组等权。alpha/l1_ratio只由内部验证选择，不用当前预测的未来结果选参。

```text
min 0.5 × sum(a_j × (y_j - beta0 - X_j beta)^2)
    + lambda × (rho ||beta||_1 + (1-rho)/2 ||beta||_2^2), sum(a_j)=1
```

来源：[Zou与Hastie](https://web.stanford.edu/~hastie/Papers/elasticnet.pdf)。系数不是因果贡献，也不是公司/经理的科学固定百分比。当前数据执行器仍只使用5项净值特征。经理、公司、新闻只有在加入可追溯的时点数据及独立验证后才能成为数值输入；不能用今天的大模型知道的结果伪造历史新闻预测。

联合情景：同步取同日期、同期限、所有候选均成熟的历史回报，保留横截面相关性，并按训练窗模型预测进行均值平移。它是经验条件分布，不是已校准的未来概率保证。缺足够共同日期则返回证据不足；不会独立抽各基金、把单基金风险简单相加，或将未来标签用来补全情景。

配置：选择买入现金金额、各批次卖出市值及保留现金，最大化情景平均净利润，并约束CVaR。代码的`tail_probability=a`表示最差尾部质量，例如a=0.05对应q=0.95；这只是风险统计口径，不是盈利概率。L_s为相对投入资本的货币损失，p_s为情景权重。

```text
max sum(p_s × net_profit_s)
eta + sum(p_s × z_s)/a <= max_cvar × (current_equity + proposed_contribution)
z_s >= L_s - eta, z_s >= 0
```

来源：[Rockafellar与Uryasev](https://sites.math.washington.edu/~rtr/papers/rtr179-CVaR1.pdf)、[含成本的组合优化](https://stanford.edu/~boyd/papers/cvx_portfolio.html)。最低申购额、禁止同时买卖同一产品通过混合整数约束处理，不把取整后的不可行金额称最优。数值输出为连续金额提案，平台金额/份额精度、实际净值和确认费用必须在执行前复算。

约束包括可买状态、限额、已确认可卖批次、申赎费用、现金和风险预算。cash包含reserved_cash子集；unsettled_cash是另计资产，不是可用现金。静态优化比较完成融资/到账后的目标，不假装已经完成交易；逐日回放另外模拟确认、在途及等待期间价格变化。CVaR不等于最大回撤，不能从“可亏25%”自动推导风险预算。无已定义风险口径时报告前沿，不编造个性化最优值。

## 资金方案与优先级

每个明确的有限追加额度保留四种求解模式，覆盖五类用户动作：

| 模式 | 可比较动作 |
|---|---|
| baseline | 原仓维持；该预算的追加资金留现金 |
| retain_positions | 不卖原仓，用现有现金或追加资金买入 |
| rebalance | 零追加时内部调仓；正追加时内部调仓与追加混合 |
| sales_only | 减少持仓、保留现金 |

不是先决定C买多少再随意选A/B卖出。所有候选在同一账户及完整约束下共同求解；每笔买入给出原现金、拟追加、预计赎回净额的资金腿，指出需减少的原仓批次、费用与等待依赖。

同预算：先判风险和集中度可行、是否超过事先确定的最低值得调整的收益改善；合格方案按期望净利润排序，再比较CVaR和换手。风险不合格的维持方案仍保留诊断，不能成为首选。无优势可不交易。

跨预算：同时报告扣除新增本金的利润、收益率、货币尾部损失和追加资金，构造三维Pareto前沿；不按绝对利润排全局名次，不把投入增加当收益。没有追加上限或资本占用偏好时，不能声称有唯一最优追加金额。funding_levels须明确包含0；其他额度必须来自用户约束或标明的有限研究档位，不私自生成无限预算。

用户允许假设追加可获得时，不追问现金来源。proposed_contribution仅用于比较；实际入金仍须用户反馈确认。追加不能被当作消除了原仓自身风险。输出必须展示零追加最佳、完整的不卖原仓追加备选以及混合方案；顶层additional_funding_alternatives只是各追加预算最佳摘要，完整候选在funding_options[].candidates。

买入腿依赖未到账赎回或未确认追加时标为等待，不能列入当前可执行买单。到账/确认后重新求解，不能继续执行已经失效的旧目标。建议不写真实ledger/current。

## 时序检验

allocation-validate对连续账户逐日推进NAV价格、订单、确认与到账，决策使用当时可得的净值代理；未确认买入只向决策层暴露已支出本金估计，不泄漏尚未知的成交份额。调用与allocate相同的compare_allocations。验证中的每个追加情景只在期初注入一次假设资金；此后不自动每日追加。同一追加金额的基准是维持初始基金并将追加留现金，不能与较少本金的基准比较利润金额。

```text
d_t = strategy_net_return_t - same_budget_benchmark_return_t
H0: E[d_t] <= minimum_net_advantage
```

均值优势使用共同估值日净收益差；尾部风险使用已经到期的h自然日组合损失，两个检验分别配置validation_policy与risk_validation_policy，不能混用日收益和h期风险阈值。

对有时间相关性的序列执行stationary bootstrap；块长采用Politis–White及2009修正公式，比较事先约定的多个块长。重采样次数按DKW误差预算及多个分布的联合误差约束计算，不固定500次。置信区间精度、预设效应检验能力、尾部样本量和适用假设均需满足，不能“8个时间块就充分”。来源：[Stationary Bootstrap](https://statistics.stanford.edu/technical-reports/stationary-bootstrap)、[块长算法修正](https://public.econ.duke.edu/~ap172/Patton_Politis_White_2009.pdf)、[DKW紧界](https://doi.org/10.1214/aop/1176990746)。DKW控制重采样的计算误差，不证明市场样本充分。

算法要求弱依赖、适当的平稳及尾部分位正则条件。未登记、选过赢家、数据不足、功效/精度不足或假设缺证据，均不能正式通过；可保留明确标注的条件诊断区间。多预算结果是多策略探索，不能挑最好者使用单次检验。当前执行器不实现SPA，也不声称有多重检验认证；若未来引入SPA，须满足其适用条件并另验最终锁定策略。原测试期一旦被查看或用于修改，属于开发资料。

当前历史NAV适配器缺少历史发布时刻、完整历史可交易性及平台条款证据，因此本入口固定为条件研究，live_prediction_allowed为false。请求中的preregistered_forward文字不构成实际事前登记证据。每日allocate产物记录真实保存时间及决策日，可积累未来审计输入，但不会据此自动获得正式预测资格。此限制不能用手改缓存标志解除。

## 安装版命令与请求

```bash
python -B <skill-dir>/scripts/research.py prepare --root <root> --plan <plan> --codes <codes...> --start YYYY-MM-DD --end YYYY-MM-DD
python -B <skill-dir>/scripts/model.py allocate --root <root> --plan <plan> --request <request.json> --research
python -B <skill-dir>/scripts/model.py allocation-validate --root <root> --plan <plan> --request <request.json> --research
python -B <skill-dir>/scripts/model.py allocation-verify --root <root> --plan <plan> --run <relative-run-path> --research
```

命令须从安装目录运行；请求文件放在所选数据根的研究区，不能修改插件内示例作真实账户。首次无数据先prepare；只有公开研究数据而没有持仓时遵守首次规则，不把研究金额当用户本金。这里退出码0表示计算/复核完成，不代表投资有效性通过。

请求结构见 [allocation-request.example.json](allocation-request.example.json)。A/B/C与金额、费率、风险参数均是接口演示，不是产品、用户账户或科学常数。使用前替换所有示例数据和参数来源。

- schema_version、as_of、horizon_days、max_feature_age_days必须明确；account.as_of须等于as_of。
- assets代码须与本次核验研究集合一致；账户批次包括lot_id、code、value、sellable、redemption_fee、settlement_days。回放额外要求acquired_date。
- assets给subscription_fee、min_buy、可选max_buy、buyable、可选max_weight；回放另给settlement_days及redemption_fee或按minimum_days递增的redemption_schedule。阶梯规则依据实际条款或明确假设，不能当前费率回填历史。
- training_policy显式给固定窗口、最低训练/共同日期、内层折数和参数网格。这些是登记协议，不是默认最优参数。
- evaluation给起止、决策间隔、计算预算和开发/前瞻标记；起始账户须按起日估值且不能带无明细的未结订单。计算预算不足报错，不悄悄缩短区间后称全程完成。
- 统计政策给置信水平、最小优势、区间精度、目标效应/功效、MC误差/预算、块长敏感性和假设审阅；风险政策另给尾部样本条件。布尔声明不是被自动证明的事实。

## 保存与复核

研究产物保存到所选root/plans/plan/research/YYYY/MM/DD/allocation-id，模型/决策/财富/模拟事件按日期分片。保存请求、源码/依赖/数据指纹和记录时间。allocation-verify从固定输入完整重算并比较结果，不编辑生成内容。研究指针可更新，真实现金流和持仓不得改动。旧实验保留原口径，不能升级成新策略通过。
