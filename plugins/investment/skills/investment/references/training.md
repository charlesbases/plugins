# 选基模型训练与验收

## 训练对象和能力边界

训练的是 [fund-selection.md](fund-selection.md) 中的统计参数与风险校准，不是靠聊天记录训练ChatGPT。日报、提示词和参考文件不是已训练模型。插件附带research.py资料准备入口及model.py实际训练、独立复核、条件预测和策略回放入口，分别见 [research-pipeline.md](research-pipeline.md) 和 [model-validation.md](model-validation.md)。参数必须现场拟合并验证，不提供宣称有效的预训练参数。

账户调仓与可选追加的当前执行方法是 [allocation-method.md](allocation-method.md)：Elastic Net预测净值总回报，CVaR优化按实际账户或声明场景扣交易费用，逐日回放与日常比较使用同一组合函数。旧train/validate-all只保留单基金条件研究基线。新增allocate、allocation-validate、allocation-verify均为明确的条件研究；当前数据适配器未取得历史真实投资资格，不将数值求解成功升级为validated。

基金净值准备调用安装目录内的research.py；训练与验收调用同目录model.py，不从数据缓存复制或临时重写程序。统计依赖按源码requirements-model.txt固定版本并加载到所选数据根目录的runtime缓存。历史交易事实缺失时只运行标明假设的条件研究，数据资格检查仍判失败；不能编造事实、系数或训练日志。模型训练状态不能由研究数据通过自动变成validated。

## 何时算有训练数据

文件夹存在、新闻摘要、净值表、日报或模型卡文字，都不等于已经有合格训练数据。原始材料与可训练样本分别检查。

新闻原文、核验状态、来源缺口与修订按 [news-research.md](news-research.md) 采集，独立新闻研究不要求先有训练模型。新闻清单完成或verified_primary不代表可训练样本合格；只有预先定义、当时可得并经特征审计和模型验收的变量才进入预测。分析结论不能直接变成任意加分、模型权重或回填历史的事实。

可训练样本须包含决策时可得X、指定h期限已成熟Y、基金组合身份、来源/标签可得时间和缺失标记。组合均值模型的Y为净值总回报，账户费用另算；下文投资者净收益与路径风险标签用于交易检验或明确声明的旧基线，不与净值目标混名。30/60/90自然日是研究期限选项，不表示所有账户的固定期限。某一标签存在不代表全部风险模型可训练。

合格历史材料可以立即构建已有成熟标签，不需要先真实买基金，也不必从今天再等90天。若只从今天开始记录，今天的X对应未来结果尚未发生，就要等相应Y及到账成熟。样本要能形成独立时间分区、覆盖目标类别并满足冻结审计/验收政策；不是机械达到某条数或几年就合格。资料不完整时只能按声明范围做研究，不能伪装成全市场或真实平台净收益样本。

## 自动首次流程与重训触发

实际首次分配、持仓复核和基金评选均先检查活动root/plan中的数据清单、成熟样本和模型卡。没有合格样本或合格模型时自动启动下面流程；采集与训练是已授权研究任务的组成部分，不重复向用户索要公开可获取的数据或要求再次手动点训练。

先从安装目录运行research.py status，并对内置产物执行verify；原来仅由缓存临时脚本生成的旧格式不冒充内置产物。无资料、执行器不匹配、已发现损坏或需要更新行情时，运行prepare创建新研究快照，保留旧记录。候选代码来自本次持仓和可核实的研究短名单，历史范围按研究目标声明；内置证据表的代码不是默认购买列表。市场标签与完整交易训练样本分别验收。新闻研究按独立新闻流程继续，不等资料或模型合格才核实事实。

先按 [storage.md](storage.md) 区分计划状态：完全首次、零计划且没有指向遗失计划的索引时，可创建稳定plan-id的研究目录、purpose=research的profile和config活动指针；不能创建真实持仓、订单或现金记录，account_initialized不设为true。之后沿用该计划，不每天新建。单一已有计划沿用；多计划无活动选择返回needs_plan_selection；损坏或索引指向遗失计划报告故障，不借首训覆盖重建。仅缺账户风险偏好不阻止独立公开研究，但个性化评分/仓位仍受其约束。

| 检查结果 | 必须推进的动作 | preparation_state / model_status |
|---|---|---|
| 文件不可读、损坏或计划选择未解决 | 报告具体问题/选择缺口，保留文件，不能删除后当首次重建 | data_unavailable或needs_plan_selection / 原真实模型状态 |
| 无原始资料 | 用实际可用工具收集公开资料，保存来源/时点/快照；列不可取得字段及原因 | collecting / 保留旧卡；无旧产物才untrained |
| 有原始材料，无合格X/Y | 清洗、身份归组、费用和时点审计、构建特征与标签；已有成熟历史优先使用 | collecting / 保留旧卡；无旧产物才untrained |
| 仅缺尚未发生的未来标签 | 保存待成熟样本和预计可得时点，下次分析增量重查；不填零或虚构标签 | awaiting_labels / 保留旧卡；无旧产物才untrained |
| 样本合格，无合格模型 | 冻结政策，建立真实执行器和可用统计环境，实际拟合、校准和验收 | ready / 新候选实际拟合开始后为training |
| 参数、验收和覆盖可核验 | 原子登记合格模型卡及产物引用，运行本次推理 | ready / validated |
| 有新成熟样本、周期到达或漂移 | 先检查冻结的重训触发；符合条件才实际重训，保存旧产物 | 依实际进度 / 依验收结果 |

preparation_state描述资料准备；实际投资者model_status仍使用untrained、training、validated、invalidated。内置model.py把研究拟合、样本外检验、风险校准和策略回放实际执行结果保存为model_validation；它不等同于合格投资者模型。历史交易条件缺失时执行条件研究，严格资格判失败并禁止实盘预测。操作失败保存阶段和原因，完整但未通过验收的实验记为rejected；不伪称成功或无限重试。

重训候选与旧模型分别留痕。旧模型保留validated/invalidated及对应范围、失效原因，不因新样本缺失而改成从未训练。只有仍有效且覆盖本次输入的旧模型可继续推理；失效者停用。新候选验收通过后才切换引用，失败不能覆盖旧产物。

训练/准备运行留存到当前root/plan的research分区；indexes/latest.json保存小型研究引用和training_lifecycle。内置准备状态放在candidate_preparation，不覆盖旧模型状态、引用或上次成功训练时间；完整交易资料缺失时该候选仍为data_unavailable。首次没有旧模型才设置untrained。保留原有财务/报告索引，不把研究流程写成成交或改变state/current.json。模型与研究引用必须在同一计划内、实际存在且校验匹配，读取时重新核验而非只信状态字。

## 运行次数与频率

首次训练是一个完整建模周期，包含有限候选参数、按时间滚动验证、独立校准和封存测试，通常有多次有目的的拟合；不是把同一参数和同一资料重复跑到结果好看。具体拟合次数由事先声明的参数空间、时间折和试验预算决定，不能以“已跑很多次”为通过依据。

每次分析运行时执行增量采集、标签成熟检查、模型适用性检查和推理；已有授权的每日任务按日推进，不新建隐含定时任务。重训频率在首次政策中预先声明：周期检查（例如月度作为工程起点）、新增有效成熟日期块、类别/平台覆盖变化或预定漂移/失效事件触发。周期与增量门槛须验证并披露，不把月度称为科学上永远最优的频率；不需要每天或日内多次拟合同一批资料。

训练数据/代码/特征与标签版本/政策及参数形成训练指纹，推理输入另外形成指纹。仅今天净值或新闻更新并不等于新增成熟训练标签。重复下载同内容的retrieved_at变化不能伪装为新样本；相同训练指纹无明确新实验或失败恢复原因时不重复训练。失败恢复保留原错误与新尝试依据，不改验收标准来让结果通过。

## 时点数据契约

所有变动事实至少保存有效日期、首次公开/可得时间、获取时间、来源及修订；历史回测不能使用今天覆盖后的最新值。候选覆盖须包含历史停售、清盘和合并事件。历史平台可买性或费率缺失时，不宣称真实可执行全市场回测。

新闻特征关联原文event_id、逐轮assessment_id、verification_status、available_at及依据，遵守新闻规范的截点与时间精度；源头修订和本地核验分别留痕，修订和分析只能使用历史决策前可得内容，不能把今天的核实结果、市场反应或新版本回填过去。未核实线索不进入事实型特征；如果研究报道或不确定性信号须单独定义、记录缺失并验证，不当已证实事件。近期新闻活跃窗口和增量重叠窗口不是训练数据跨度。

| 表 | 必须字段 |
|---|---|
| fund_identity | share_code, fund_group_id, share_class, type, benchmark_id, company_id, inception_at, termination_at |
| nav_events | share_code, nav_date, nav, distribution, split_factor, published_at, retrieved_at, source_id |
| platform_terms | platform, share_code, effective_from/to, available_at, buy/sell_status, limits, min_amount, cutoff, calendars, confirmation_rule, arrival_rule |
| fee_rules | fee_rule_id, share_code, platform, amount_tiers, holding_day_tiers, subscription/redemption_rule, discount_scope, effective_from/to, available_at |
| disclosures | fund_group_id, report_date, published_at, exposures, duration, credit, leverage, concentration, size, manager_ids/tenure, source_id |
| features | decision_at, share_code, horizon_days, feature_version, values, missing_flags, max_source_available_at |
| labels | decision_at, horizon_days, label_capital, fee_rule_id, subscription_at, entry_nav_date, redemption_at, exit_nav_date, cash_available_at, net_return, terminal_loss, path_loss, drawdown, label_available_at, censored_reason |
| run_manifest | run_id, dataset_hash, universe_version, split_boundaries, preprocessing, hyperparameters, coefficients, calibration, acceptance_policy, model_status, metrics, artifact_hashes |

平台是时点条件：从当前计划及最新反馈读取已确认平台，记录确认来源和时间；天天基金等案例不是其他账户的默认平台。研究样本按各自声明的平台场景核验。平台变更后重新核对可买性、费率和模型覆盖，不能复制其他平台折扣。公司/经理资料也须带首次可得时间，共同任职和变更要能重建。

## 标签和交易模拟

以下描述投资者交易结果的审计目标。新增组合模型用净值总回报拟合，执行回放按下列费用原则核算；不会用统一假设费率拟合所有账户的个人净收益。条件数据只能输出标明假设的研究结果。

1. 固定决策时刻与h的自然日定义，特征只使用decision_at之前已可得资料。晚间发布净值或新闻不能用于假装当天15点前已下单。
2. 依据实际产品/平台日历、截止时间和规则模拟下一可执行申购、确认、h到期赎回及到账，保留期间现金占用。QDII等日历不能套用普通境内T+1。
3. 净值总收益处理分红和拆分，已反映的管理/托管/销售费不再重复扣；个人申赎、佣金及其他适用费用按历史条款另计。
4. 净收益以同一研究金额label_capital计算，关联金额档与fee_rule_id。研究金额评分不等于最终配置金额的实际净收益，组合层重算。
5. 终点负收益terminal_loss、从投入本金起算的路径最大损失path_loss、从峰值起算的drawdown分别建标签。仅终点分布不能回答路径最大亏损。
6. 期限内无法赎回、缺价或资料不足者记录不可执行/删失理由，不偷偷延长标签、插值填成完整收益或删除失败基金。若采用删失估计，方法及限制另行验证。

## 前向训练流程

先冻结研究目标、候选范围、期限、标签、基准、风险定义、试验次数及验收标准，再运行；不要看测试结果后调标准。

```text
原始时点表 → 可得性/候选质量审计 → 特征和成熟标签
按唯一决策日期切训练/校准/验证折 → 均值模型拟合
前向损失/分位校准 → 完整选基和交易回放
封存最终测试 → 模型卡验收 → 日常推理或降级
```

- 按唯一决策日期分区，每日全部基金属于同一时间块；不随机逐行拆分。每次训练、内层选参及校准都声明as_of；它们使用的标签必须在该阶段首次预测决策前已成熟。尤其训练标签不得跨入随后的校准预测起点，校准标签也须在后续验证/测试推理前成熟。按实际label_available_at清除跨界未来标签，不能只检查训练到验证之间的隔离。最大90天加交易/到账延迟只是隔离参考。
- 各折内单独拟合缺失处理、标准化、相关特征筛选、超参数和校准；不得先用全数据归一化。A/C等组合共享样本去重复或控制重复权重，不把相关份额当独立证据。
- 按基金类型与h拟合第一版Elastic Net均值基线，用训练/验证期确定正则化和变量保留，不默认以参数默认值为最佳。公司、经理效应只能使用当时已经成熟的历史；若宣称适用于新经理，另做经理留出检验。
- 均值之外，使用独立前向校准残差分布或分位模型，报告覆盖率、分位损失及尾部超限。分位预测自身不证明区间校准，样本不足时只列情景，不报概率。用分位推算完整损失期望时须说明尾部外推/积分近似并验收，不能从几个分位点冒充完整分布。
- 处理高度相关指标，按指标组做消融或留出重要性检查，报告不稳定性。系数/重要性是模型关系，不是因果贡献或永久的因素百分比。
- 用未参与拟合/选参的时间段测试全过程。记录所有尝试的变量、参数与模型，控制反复试验的选择偏差；最终测试不能反复拿来选新方案。

数据少时可以先做研究基线，但不能因行数多就称样本充分。大量重叠90天窗口、份额与共同市场暴露会降低独立信息量。历史年度数、折数和窗口长度应依据样本与市场覆盖预先声明，不能用一个固定年份门槛替代论证。

## 验收和降级

验收政策须在测试前冻结，并有具体指标、阈值/容差、置信规则及理由；留空政策不可判通过。风险参数属于账户目标，未明确时不能从回测收益最大值自动生成lambda，或将25%解释为已确认路径/概率限制。

| 层次 | 核查及通过依据 |
|---|---|
| 数据 | 无前视、费用重复、未解释分红拆分；缺失、覆盖及历史候选可审计 |
| 预测 | 分类别/期限报告净收益误差、相对同类基线表现；不能只报整体相关性或训练拟合 |
| 风险校准 | 区间覆盖、分位损失、终点及路径超限按相应标签检查；失败的概率输出停用 |
| 策略 | 模拟准入、评分、金额、交易、到账、延期及换仓；与同候选/费用/约束的低成本同类、静态配置和维持方案比较 |
| 优势声明 | 若声称存在优势，预先指定的费用后改善需在按日期块估计的置信检查中成立，并符合已确认风险目标；否则只报研究结果 |

历史平台数据缺失降级为假定可执行的研究回放；新类别/期限/平台不在覆盖内降级为未评分。概率校准失败不报告可靠概率；参数、数据漂移或治理事件触发预先声明的失效规则，状态变invalidated。不能一边未通过，一边输出“最优基金”。

## 产物和日常使用

沿用 [storage.md](storage.md) 当前确认的数据根目录（默认`$HOME/.investment/`）及活动plan-id，数据、脚本、参数和模型卡写入`<root>/plans/<plan-id>/research/YYYY/MM/DD/<run-id>/`。训练、预测、模型卡和原始资料的全部读取/写入使用同一root及plan，不因默认路径切回其他账户，遵守分区与大小规范。模型卡保存类型/期限/平台和费率覆盖、决策与标签版本、训练/校准/验证/测试日期、数据/脚本/参数校验值、目标函数、校准、验收政策及实际结果、状态和失效条件；细节分文件，不将整个数据集塞入卡或摘要。

未运行为untrained；执行中为training；只有实际产物可核验且冻结验收全部满足才为validated；范围不匹配、过期或失效为invalidated。模型卡并不是资金/成交台账，不初始化或改变真实持仓。

日常更新可得数据与预测，读取卡及相关分区；不每天因排名变化重新选参。按事先声明的更新周期或漂移条件重新训练，保存旧产物、理由及回归结果。训练工具/数据无法运行时保留旧模型真实状态和候选进度，说明缺口；无旧产物才记为untrained。

## 方法依据

- [Elastic Net](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.ElasticNet.html)：均值回归与正则化方法，不证明基金收益可预测。
- [QuantileRegressor](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.QuantileRegressor.html)：分位回归方法，不自动提供已校准分布。
- [TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)：时间顺序与gap工具；本规范另要求日期面板分组及实际标签成熟检查。
- [回测过拟合研究](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)：反复试验与事后选参须纳入验收。
