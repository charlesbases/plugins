# SC09 内部流程：建议映射与发布核对

文档v1；producer开发阶段。已接受的独立验证器保持冻结；生产流程、标准process/run/verify入口与正式publication_assessment提示词已落盘，待fresh生产流程复审。真实Host与四个完整selector尚未执行。

[外部说明](README.md) · [精确规范](../../spec/v1/sc09.md) · [公共契约](../../contracts.md) · [项目约束](../../../AGENTS.md)

1. **冻结与绑定。** run_sc09进入共享controller，先核正式/本批命名空间再读输入。冻结StageInput、method、提示词/Schema字节身份、真实时钟与attempt预算。输入role最小读取reporting，控制器核准确本run manifest→attempt→seal→candidate/receipt→导出引用；本链不重复上游业务验收，另核policy/actions/account/qualification/protocol相互一致。
2. **自动取得真实发布材料。** 宿主从已收到任务/登记记录及准确官方来源组装PublicationContext，保留Capture原字节/hash/HTTP与FetchAttempt失败、Citation实际支持范围。账户与偏好只核本轮记录及真实已收到变化，不声称未连接的外部账户无变化、不新增账户余额与对账，也不要求用户重复确认原事实。公开来源可取则实际取，取不到保留对象/Gap，不固定unknown或按测试名跳过。
3. **必要条件全集。** 独立从原动作、账户、用户参数、合同/窗口、资格及适用恢复政策导出应检查集合。所有必要发布条件都登记，不接受producer自行required=false或省略未取得项。真实新事实只判变化与责任方，不替换决策输入；不新增同run再执行SC01的私有分支。
4. **确定性映射。** 全current_actions恰一ActionMapping；所有非hold可执行指令完整映射或整体观察，不能只执行政策一部分。申购金额/赎回份额原精度准确复制。逐批次依FrozenPolicy.selected_rule对应AllocationDimension与SC01确认/锁定记录及StateRow重建，保留原lot/fee tier与守恒；不自行选择不同批次。合并展示须仍可还原原明细和经济效果。
5. **精度与估计。** 独立验证源合同步长/最小/余额/舍入与原指令；非法则返回SC07，不在发布环节改数。仅按真实适用公式计算费用/参考份额/净到账Estimate，原NAV日期/时点/假设可核；未知保持null+reason，费用不猜0。FundingConditions只表示未来到账/贡献条件，不能当当前预算。
6. **时间交集。** 检查finite/event_contingent/unknown条件及准确端点，独立重算最早有限界和并列exclusive优先，交集必须非空；SC08资格界必纳入。真实新账户事实、申赎公告等事件条件保留，不能转成无限保证。未知必要界不能给默认TTL；发布复核时钟不冒充实际交付时间。
7. **组装与独立验收。** Candidate导出五表及真实业务证据，维持原范围/未知/模拟标记。独立validator从输入、原文、公式和冻结规则重算R01—R18，不调用producer业务算法，不读producer私有host记录。receipt由validator生成、许可由冻结use_rules决定，controller seal后才交SC10；SC10消费时核仍在边界内并记录实际交付。

两角色正式Host步骤登记publication_assessment与publication_review。工作目标为global，以及完整原动作、原成员/持仓、实际必要发布条件；目标先冻结排序再按host_batch_size分片，不因缺口/预算删目标。global规则R01/R02/R15/R17/R18，动作规则R05—R09/R11/R14，处置R10，发布条件R03/R04/R12/R13/R14；不适用项须明确依据，保持准确规则覆盖。每个target_id用前缀g-/a-/d-/p-加SHA256(公共canonical UTF-8 JSON tuple)构造：global=["global",完整StageInput引用]；action=["action",完整current_actions_ref,action_id]；disposition=["disposition",object_kind,scope_object_id,完整source_object_ref]；publication=["publication",完整PublicationContext引用,check_id]。每批findings的(target_id,rule_id)唯一，精确覆盖该批目标及上述规则；不能裸ID猜测或压缩掉未通过项。

work_key为global或action-batch-0001…、disposition-batch-0001…、publication-batch-0001…；编号至少4位、超过9999自然扩展。两角色分别实际新上下文，不复用对方原文。host_work_budget在请求前核两角色全部registered（含pending/failed），不足真实失败而不删原范围；actual_host_work_item_count仅取本attempt completed_count(producer)，不等于物理API调用数。无真实HostResponse等待host_work_required，不填造响应。路径登记不代表当前已有正式prompt。

变化/证据不足仅保留诚实observation_only报告、零指令及原动作映射/范围；账户变动归SC01，资金/偏好归独立任务输入，合同/状态/窗口归SC05及受影响SC07/08。SC09不回写上游，不预设任何旧产物过期，不凭恢复名称重置三次预算。risk恢复独立消费接口与StateRow/冻结批次规则的完整来源追踪在报告说明；未闭环部分不得声称已支持。

所有正式产物按共享runs目录与引用生命周期保存；本链无独立账户DB、余额表或全产物永久库。正式root严格~/.investment；测试/联网/临时/辅助均~/.investment/test/<batch>/，安装规范只读。静态校验只证明结构；真实四selector须在上游合法业务输出可获得后实际运行process+validate，不伪造Seal或用缓存成功替代。

本链SC05读取仅限准确当前市场快照及原动作所需contract_vintages/真实合同原文；不消费DataView/current_view、panel、labels、independent_view，不借GridRule子引用旁路SC05 whitelist。

本链decision_at准确等于本链StageInput.decision_at；原动作的经济决策时钟沿准确上游对象保留，不重写为发布时间。publication_checked_at等于冻结Parameters与PublicationContext.reviewed_at，覆盖本轮实际材料复核完成时刻且不晚于本链decision_at；旧预测cutoff保持原义，较新资料仅核适用性。host_batch_size/host_work_budget是有真实basis_refs、determined_at及engineering_budget来源的正整数count，冻结后不变、不套默认值，不充当科学阈值。

当前验证模块位于src/investment/chains/recommendation：contracts只读快照，deterministic核准确映射/批次/范围，finance独立Fraction核算与来源精度，temporal核真实材料及必要时间交集，semantic_review规划全部原目标并核实际宿主原文，validation分发全部R01—R18并按冻结用途门形成receipt。验证器不调用producer算法、不签Seal；标准脚本为scripts/validate_sc09.py。

基础校准入口：冻结validator的tests/test_sc09_validation.py与producer的tests/test_sc09_flow.py分别保留独立范围。后者仅含已批准的申购实付外扣算术、确认lot首步份额守恒、最早exclusive交集及原日期/资金条件投影；运行前SC09_FOUNDATION_ROOT须指向本批测试根，以PYTHONIOENCODING=utf-8、PYTHONPATH=src和.venv/Scripts/python.exe -B执行。全部辅助来源写入该批；基础检查不创建真实上游/Host/Selector成功，CLI帮助也只是接口检查。

生产模块独立于validator业务算法：production_inputs绑定本轮六角色与opaque上游用途；production_finance从原指令、lot和合同推导；production_publication保留已收到变化、原时间精度及资金条件；production_sources规划全目标并核当前attempt的真实评审；chain只组装五表与Candidate。run_sc09复用原Controller的三次失败预算；verify_sc09接收本批实际StageInput，不自造上游或宿主响应。真实HostRequest由主Codex宿主以正式提示词新上下文处理后续行；不要求用户填内部JSON。

来源不足按字段保留：已有历史卖出而SC01无法唯一归属剩余lot时不猜历史FIFO；合同有限生效截止缺上端点包含语义时不据TimeEvidence上界补端点；SC07 earliest_cash_arrival_at仅有保守索引时不冒充物理到账时刻。已知金额/份额/费率合同实际计算，无法唯一证明的时间或分配留下具体Gap，并阻断依赖它的建议。完整selector需要本批真实SC01/05/07/08与PublicationContext，启动前已terminal的调用不计作本次重新执行。

来源不足保留具体限制：未知费阶/精度不猜数；未登记可执行算法的documented_formula、非calendar_days持有日计数、不能证明按单收费如何分配至多lot的固定赎回费，均返回明确不足。日期证据不补成精确午夜，必要边界未定不生成TTL。实际SC01/05/07/08同批产物、SC09 producer与正式模型通道就绪后，才运行四个完整selector；当前数学/源码校准不能替代该准入。
