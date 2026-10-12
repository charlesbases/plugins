# SC08 内部契约：独立资格评价

文档版本v1。状态：仅契约/科学依据阶段；本页编排及provider消费提案尚未实现，不声称流程、校准或历史持久化通过。

[外部说明](README.md) · [精确规则](../../spec/v1/sc08.md) · [method](../../spec/v1/method-sc08.json) · [项目约束](../../../AGENTS.md)

## 正式调用链及独立责任

计划调用为run_sc08 → ResearchController冻结输入/方法/Schema/提示词和attempt → process_sc08/produce → 实际HostRequest/宿主新上下文 → 完整CandidateOutput → validate_sc08/validate独立金融/统计计算及原文审核 → 共享许可seal或失败反馈。首次+2次失败后停止后链，输入、方法、时钟、独立资料身份和已用预算不因重试重置。宿主没有响应则host_work_required同attempt续行；真实失败保存event/raw可得状态与failed response，不能无限等待或造成功。当前本阶段不创建这些Python或正式提示词。

1. 入口先校根/mode及有序六role、上游真实Seal/用途与Parameters/协议绑定。准确读取上游低层报告权限与opaque receipt；不重复上游业务验收。独立评价所需高用途不足留Gap。SC07完整PolicySpace、Optional selected Rule、当前动作Dataset与SC06 ModelRule均分别绑定。
2. 冻结完整origin×horizon、claim×assumption及原scope对象计划。治理参数/规则实际登记时间不伪装历史；经济资料、合同、身份和用户事实分别沿origin/C。资金不是余额。provider预约未就绪时不读取independent view或可能含其结果的raw，只保留not_opened、准确控制引用、原范围/协议允许的缺口工作。
3. provider就绪后原子预约精确claim，再登记首开，才开放相应资料。每origin在SC08受限评价上下文实际执行冻结模型、选择/再规划和更新规则；更新仅用此前成熟获准样本。每次ReplayComputation保存实际时间、完整训练/选择/预测/情景/政策评价及合法成熟集合；不能拿今天金额或旧成功缓存代替。原定规则内选首步与利用独立结果换总体胜者必须区分。
4. 本链producer按原合同独立于SC07重建账本，并按Objective.cash_interest_rule及准确cash_interest_basis_ref独立记录cash_interest，未知利息不得默认0；本链validator从输入和来源另推公式/状态，不调用任何producer金融/统计业务算法。它可共享Schema、引用解析和精确数值基础能力。模型回放输出采用已冻SC06结构与数值标准，属于本链新计算，不签上游Seal。
5. 完整同账户/资金/币种/期限/财富口径配对。逐metric计算成熟预测诊断；逐claim用冻结MBB同步向量索引，精确重算均值/原子CVaR及向外区间。依赖/稳定/矩/密度/尾部与滚动政策适用性由实际原证据和独立宿主评估，不以不拒绝或固定N代科学证明。缺/未成熟/未比较全部保留。
6. 回到原目录/分页及来源、预测和计算记录核U/R/A、未知/可能/未处理域和全部真实风险；逐R外原证明绑定本任务目标/约束，不能信producer confirmed。聚合成QualificationVerdict，独立验证Q01—Q18及完整AcceptanceRule，公共receipt才授用途。

## 固定Host工作计划

协议origin按Instant排序去重，完整scope键按规范(object_ref,object_id,peer_group_id或不适用)排序；host_origin_batch_size决定固定连续分片。两角色分别有global、origin-batch-0001…、scope-batch-0001…，编号从1、至少4位，超过9999扩展；不因失败、测试名或预算换键/改分片/删名单。producer步qualification_science产ScientificAssessment；validator步qualification_review产独立ValidationReview。每个origin批精确覆盖该批全claim×assumption，每个scope批精确覆盖全部scope目标Q14；global核协议/规则和全局科学缺口。非该批目标、重复或漏项均拒绝，原样保留每finding的origin_ats。

所有global/assumption/scope target_id及逐批精确覆盖集合按sc08.md的g-/a-/s-加规范tuple SHA256定义，不能按模型自填名称或producer删改后的清单猜目标；(target_id,rule_id)唯一，scope_check_id与s-目标相同。

ScientificAssessment正式汇总的work_kind=aggregate，合并所有实际批目标及发现而不删缺口。原HostResponse.output_ref及raw仍私有；producer将允许公开的派生分析写入本attempt正式science记录并注明模型分析属性，validator回原来源与自身Host工作独立判断，不能把副本当原始事实。宿主不代替Python独立账本/统计重算。尚无provider时global可如实分析缺依赖，origin工作不执行。

预算从两角色ctx.host_work_summary().registered_count()核，pending/failed不释放；actual_host_work_item_count取本attempt completed_count('producer')。全预定工作预算不足如实失败/不足，不改范围。仅真实调用的新上下文/事件/原文/响应可算完成；本阶段未产生任何此类成功记录。

## Task10共享历史provider的精确消费提案（未落地）

以下为后续owner需冻结的字段和事务语义，不是已注册schema、可用函数或SC08私有存储。接口/schema未由共享owner实现并审核前，Parameters.test_history_ref必须unknown且无resolver grant，不能凭伪造Ref导入快照。仅保存跨轮family/claim预算和暴露身份，不永久保存全产物、原文、账户或完整业务输出。

| 对象/字段 | 强类型与关系提案 |
|---|---|
| family | family_id=公共ID；canonical_family_id=公共ID；parent_family_ids=唯一ID数组；history_generation=非负整数；allocation_rule为SC05原闭合联合；family_error_budget=0<A<1的有限ScientificDecimal；protocol/model/policy族治理来源为准确Reference数组。别名/新run不能新建空家族；关联完整性无法证明不得授独立资格 |
| claim identity | claim_id=公共ID，metric_id/horizon_id=公共ID，baseline_id=公共OptionalID，acceptance_rule_ids=非空唯一ID数组；protocol_ref/model_rule_ref/policy_ref/view_ref为准确ArtifactRef，actions_ref如需绑定为完整DatasetRef；冻结字段必须逐字节身份相等，不能只比名称 |
| sequence/budget | reservation_id=公共ID；global_index=j>=1的严格integer（非bool）；family_position为Optional正integer；K仅familywise已登记family_size正integer且ordered_claim_ids完全等长唯一；alpha为有限十进制/正有理计算按协议精度floor结果；confidence=1-alpha；同时保存原算法ID、A、精度和allocation依赖，供SC08独立重算，不能由SC08自填j |
| time/status | reserved_at为真实Instant；first_exposed_at=OptionalInstant；finished_at=OptionalInstant；state=reserved_unopened/exposed/completed/failed/insufficient_evidence/cancelled_unexposed/cancelled_exposure_unknown；实际run/invocation/attempt身份保留以审计，不作家族重置键 |
| exposure identity | 精确view Ref及其实际partition/sample_ids/label_ids/来源版本身份摘要，opened_evidence_identities为唯一(schema_id,sha256,byte_size及受控定位)集合；只记录身份/必要成员，不存正文。重新导出/换URI/换view名而底层标签重合不得当新未触及资料 |
| snapshot | snapshot_id、provider_id为公共ID；generation为非负integer；snapshot_ref为真实不可变授权Reference；含完整family/相关claim及既往exposed集合，previous_snapshot_ref为OptionalReference。已知空历史必须由授权provider真实穷尽读取和新建事务证明；本链不能自己填空 |

逻辑唯一键至少为canonical_family_id、(canonical_family_id,claim_id)、(canonical_family_id,global_index)、reservation_id及同snapshot内exposure identity；同claim的完整冻结identity不允许变更。allocation_rule/A/K/ordered_claim_ids在家族首次开放前冻结，不接受从已见结果重排或缩K。需要哪些旧family/claim由稳定治理/继承关系确定，不能仅按本run检索；完整性无法核实时输出不足。

事务调用前提与转移如下：先读取真实最新快照并核generation；原子reserve在同事务中判同claim恢复或新claim、分配唯一下一j、冻结identity/alpha并提交不可变预约；恢复返回原reservation/j/alpha而不再消费新序号。独立view首次读取必须经provider先原子登记exposed，提交后才授该准确资料的SC08读取权；失败中断后不能倒写unopened。若正常API无法把登记与读取门结合，SC08不能仅凭自己写的access日志证明未提前开放。

reserved_unopened→exposed→completed/failed/insufficient_evidence是正常路径；reserved_unopened只有真实证据证明未开放才能cancelled_unexposed，无法证明为cancelled_exposure_unknown；exposed后的取消仍保留暴露消耗，不能转回unopened。任何终态都不删除序号/预算；同claim重试恢复原身份并追加实际执行事件，不覆盖旧曝光。新claim需要新预约/j；已开放后自适应新方法复用相同或重叠标签不是独立评价，不能仅分配新alpha放行。SC08/validator读取本轮及前轮真实快照核对，不能用手写假历史演示成功。

## 校准准入与独立预期

| selector | 真实入口/合法触发 | 独立预期及当前状态 |
|---|---|---|
| independent_ledger | 隔离批先真实SC01登记合法10份买入，原合同明确NAV10/赎回1%/锁定及确认到账；SC07冻结合法卖出规则，FundingPlan明确上限0，再实际SC08 process+validate | gross100、fee1、应收99，确认后份额0/可用0，到账后应收0/可用99且不二扣。全部来源保留synthetic_fixture，本阶段not_executed |
| paired_evaluation | 真实provider先预约开放、原origin成熟且合同/初始账户/基准相同，实际冻结规则与独立账本回放；三origin只校算术 | 若实际路径财富政策110/90/105、基准100/95/100，则均值差10/3 CNY，B=100/beta=.5时CVaR=5；按真实持久化索引独立重算每draw/分位。三点不授科学充分；本阶段not_executed |
| insufficient_evidence | 合法协议范围包含未成熟/availability未知origin；另合法合同场景有已证到账晚于现金期限 | 前者insufficient并保留对象，后者金融failed/blocked；区间跨门槛是不确定。缺provider不开放资料；本阶段not_executed |
| repeated_test | Task10真实provider就绪后先实际一轮，再新run/同claim恢复，真实读取前轮记录 | 同claim保留原j，新claim真实下一j，保留开放/失败/不足/取消未知暴露；已开放后自适应同资料拒绝；当前依赖未实现，不手写历史，本阶段not_executed |

源模拟真实性与模型执行真实性分别报告：synthetic_fixture即便经过真实模型调用也不是外部金融来源或真实用户收益。每selector须先取得真实可达合法上游，再执行本链process+validate及必要宿主工作；不能伪造上游Seal/成功模型结果、只运行共用算法的循环证明或写测试专用提示词。独立预期与实际执行证据分别保存，静态Schema/手算不是selector通过。

## 运行、隔离和保留

正式根仅~/.investment；全部测试/联网/缓存/辅助根为~/.investment/test/<batch>/严格后代。读取/建目录/锁/数据库前先校mode/root。正式入口不能消费测试数据，测试不能连接正式账户库。所有终端显式Bash；业务Python在插件目录以PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe -B运行。

本attempt候选、十表、访问/统计/原范围证据及实际宿主材料写runs/sc08/YYYYMMDD/invocation/attempt目录；共享manifest与同级README登记真实调用/状态/保留条件。跨轮只由Task10维护上述必要family/claim预算与暴露身份，不因本链使用过而永久存全部产物、原文、账户表。有效准确引用依赖的runs字节保留到许可/审计依赖结束，清理不能破坏有效用途。重试/恢复不把缓存成功当本轮真实调用。

当前交付只含本链六份契约/说明与任务report；不声称任一Python入口、验证器、业务校准、宿主提示词或共享provider已实现。后续先根审核契约，再根派工validator及独立校准；冻结后才写producer/实际流程，最后fresh review。
