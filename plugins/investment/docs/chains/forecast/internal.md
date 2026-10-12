# SC06 内部设计：预测与联合情景

状态：validator已作为producer开发基线接受冻结；production_inputs/production_numerics/production_fits/production_scenarios/production_diagnostics独立核心及正式forecast_assumptions模板已落盘。原四项数学/方法基础及同端点明确Failure的方法校准通过，总新增实际ElasticNet.fit一次；这不是fit_tasks、正式生产编排或四个完整selector的真实执行。producer当前日历消费与正式chain/process/run尚未接入；真实SC05 Seal/Context、正式两角色Host和ForecastBundle仍未产生。精确类型、关系、数值和权限以 [sc06.md](../../spec/v1/sc06.md)、[Schema](../../spec/v1/sc06.schema.json)、[方法](../../spec/v1/method-sc06.json) 为准。

## 执行链与边界

run_sc06 → 共享控制器冻结输入/方法/提示词/Schema/attempt → process_sc06/produce → 必要真实HostRequest → CandidateOutput → 独立进程validate_sc06/validate → receipt → 共享Seal或反馈重处理。最多三次，不造平行外壳、不重签上游事实、不自签成功。

1. 读两角色及真实opaque上游凭证，沿Bundle读protocol和三获准物理视图；审计本体用于保留缺口，不展开祖先raw。先绑定U、mandatory风险和全体成员处置。
2. 按冻结predictors及applicability构造每实体P(e)，保留列顺序/unknown槽并核panel/fixed源实体、SourceRule本体、单位及变换；不从current反推列，历史退出成员无需current行。随后按协议派生entity/horizon、origin和网格任务，记录成熟/未成熟/排除集合、窗口与隔离。历史选参只用此前成熟结果，不把最终赢家写回历史。
3. 每折真实拟合训练均值/总体方差、ElasticNet，独立记录training_mean；保存模型数值、输入身份和全部候选预测/失败。计算预算不删未知成员。
4. 用完整前向SSE/n及冻结顺序选择，按获准集合拟合当前模型。记录当前选择界、真实冻结时钟和历史origin；冻结更新规则供后链使用。
5. 建成熟OOS残差及完整向量清单，缺口切断连续段；固定随机算法抽合法块，逐向量生成共同scenario。Snapshot按partition保留获准CalendarRows；F13只以current行、原Horizon及全部源时区解核required实体同目标日与完整period，upper作索引，NA/unknown不顺延。历史仍核各origin当时资料/实测口径。C/h端点、R=mu+e、全部索引/概率及经济无效路径保留，U和风险不缩小。
6. 按协议DiagnosticSpec生成每拟合/共同残差数据集的ordered_origin_block_inventory；producer宿主forecast_assumptions逐项documented_assumption_review，记录source_scope_match/冲突/未决/conditional_support。无CNY财富loss的尾部清单保留SC07/08依赖，不用残差代替。使用冻结模板和真实新上下文/原文；缺响应保持host_work_required。
7. validator从获准原输入独立重建训练集合/统计、P/D/KKT、预测/MSE、残差/块索引、路径/概率和全U补集；不调用producer业务算法。forecast_review新上下文独立审查适用证据，不读producer私有宿主材料，不用模型投票代替数值核算。

## 阶段与持久化

根审核契约后才派validator-only，独立校准forward_fit、immature_labels、joint_scenarios、member_gap；根冻结验证器后再写producer和真实流程单测。独立手算预期、合法上游流程、模拟来源和所有辅助文件保留在本批 ~/.investment/test/<batch>/，不伪造Seal。

当前 validate_sc06.py→共享stage_main→validation.validate→Snapshot/Checks→F01—F18确定性/独立数学/同步场景/三类诊断→真实forecast_review或用途凭证。数学基础unittest校准独立P/D/KKT、参考审计、尺度、合法块与RNG API，另有公共不可变工作摘要值对象接口检查；不能冒充上述四个完整selector。F09/F17通过ctx.host_work_summary()分别核producer成功数及两role登记预算，请求前预占新增键，控制器登记后的恢复验证再核实际预算及完成键；摘要缺失不当作零，身份不匹配保持failed。F13预计期间输入缺失或不同口径仍明确不足，保留原期限和风险；开发基线冻结不等于真实业务或当期适用性验收。

模型为可重读JSON数值，记录实际库版本和输入引用；审计/情景为本attempt不可变对象，不使用pickle或新增永久库。同attempt正式Host等待/续行允许经现有resolver准确重读本attempt已真实完成、原输入/方法/数据/任务身份绑定的产物；不跨attempt复用模型或失败候选，不把半写入或冲突文件当已完成。当前尚未接入并真实执行此生产续行编排。

verify_sc06.py将数学/方法基础（原四项及同端点明确失败校准）与ActualSC06Flow四selector分开。真实案例先读取独立预期和原源hash，再走同批SC04/SC05真实Seal→ResearchController的实际process/validate与正式Host续行；没有合法case时准确not_executed/exit4，不造Seal/Context/响应。现有准入检查尚没有启动任何SC06业务attempt。待确认的asof日历增量不提前消费；早期origin的前向选择不足按原事实保留，冻结validator未为生产结果放宽。

科学门、失败原因和用途逐规则登记。没有真实拟合、宿主运行或校准证据时，不以本说明或静态Schema装载宣布SC06通过。
