# SC06 内部设计：预测与联合情景

状态：仅契约阶段，producer/validator及实际校准待实现。精确类型、关系、数值和权限以 [sc06.md](../../spec/v1/sc06.md)、[Schema](../../spec/v1/sc06.schema.json)、[方法](../../spec/v1/method-sc06.json) 为准。

## 执行链与边界

run_sc06 → 共享控制器冻结输入/方法/提示词/Schema/attempt → process_sc06/produce → 必要真实HostRequest → CandidateOutput → 独立进程validate_sc06/validate → receipt → 共享Seal或反馈重处理。最多三次，不造平行外壳、不重签上游事实、不自签成功。

1. 读两角色及真实opaque上游凭证，沿Bundle读protocol和三获准物理视图；审计本体用于保留缺口，不展开祖先raw。先绑定U、mandatory风险和全体成员处置。
2. 按冻结predictors及applicability构造每实体P(e)，保留列顺序/unknown槽并核panel/fixed源实体、SourceRule本体、单位及变换；不从current反推列，历史退出成员无需current行。随后按协议派生entity/horizon、origin和网格任务，记录成熟/未成熟/排除集合、窗口与隔离。历史选参只用此前成熟结果，不把最终赢家写回历史。
3. 每折真实拟合训练均值/总体方差、ElasticNet，独立记录training_mean；保存模型数值、输入身份和全部候选预测/失败。计算预算不删未知成员。
4. 用完整前向SSE/n及冻结顺序选择，按获准集合拟合当前模型。记录当前选择界、真实冻结时钟和历史origin；冻结更新规则供后链使用。
5. 建成熟OOS残差及完整向量清单，缺口切断连续段；固定随机算法抽合法块，逐向量生成共同scenario。C/h端点、R=mu+e、全部索引/概率及经济无效路径保留，U和风险不缩小。
6. 按协议DiagnosticSpec生成每拟合/共同残差数据集的ordered_origin_block_inventory；producer宿主forecast_assumptions逐项documented_assumption_review，记录source_scope_match/冲突/未决/conditional_support。无CNY财富loss的尾部清单保留SC07/08依赖，不用残差代替。使用冻结模板和真实新上下文/原文；缺响应保持host_work_required。
7. validator从获准原输入独立重建训练集合/统计、P/D/KKT、预测/MSE、残差/块索引、路径/概率和全U补集；不调用producer业务算法。forecast_review新上下文独立审查适用证据，不读producer私有宿主材料，不用模型投票代替数值核算。

## 阶段与持久化

根审核契约后才派validator-only，独立校准forward_fit、immature_labels、joint_scenarios、member_gap；根冻结验证器后再写producer和真实流程单测。独立手算预期、合法上游流程、模拟来源和所有辅助文件保留在本批 ~/.investment/test/<batch>/，不伪造Seal。

模型为可重读JSON数值，记录实际库版本和输入引用；审计/情景为本attempt不可变对象，不使用pickle或新增永久库。缓存绑定数据/时点/许可/特征/目标/方法/随机性，不能用旧结果冒充本轮执行。

科学门、失败原因和用途逐规则登记。没有真实拟合、宿主运行或校准证据时，不以本说明或静态Schema装载宣布SC06通过。
