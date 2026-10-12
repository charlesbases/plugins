# SC10 内部流程：完整报告、独立预核对与真实接收

版本v1；当前只有契约/依据，无业务运行证据。

[外部契约](README.md) · [精确字段与规则](../../spec/v1/sc10.md) · [方法及用途门](../../spec/v1/method-sc10.json) · [项目约束](../../../AGENTS.md)

1. run_sc10调用共享controller，先校命名空间/root/mode，再冻结StageInput/method/prompt/schema和首次+两次预算。ResearchSchemas显式加载SC03—SC10前缀，不扫未完成资源。缺必要账户/范围或上游耗尽预算停止，交根控制出口。
2. process_sc10只消费本轮具reporting许可的准确业务payload及主调度ReportingContext；共享controller核manifest→Seal→candidate/receipt，业务validator核账户/范围/祖先对应，不重执上游业务。不得读取旧缓存、latest或未授权SC05训练/heldout视图。
3. 从原已登记完整表生成无损附件索引、十个完整集合快照、覆盖和资格摘要。成员按原主键和member_id分别核，未知保持unknown；原上游每表全行全字段随报告交付，索引不代替实际字节。
4. producer的report_composition HostRequest由Codex宿主真实新上下文组织带来源的中文claims。依据工程预算批量处理全target，汇合后渲染最终Markdown，生成精确UTF8 byte span Claims。金钱/份额/费用/排名/条件沿源，不新算投资结论。
5. producer的report_precheck在另一个真实新上下文读取固定实际Markdown+完整附表+原允许来源，PrecheckFinding恰覆盖实际targets×{T01,T02,T03,T04,T05,T06,T07,T08,T09,T15}，缺/多/重复/错阶段均不能passed。T08用真实checked_at核原来源时点和当时可核条件，T09读取固定报告与所有实际原附件/索引/claims及全部内容依据，核span/原表全行全字段；不读取未来delivery.completed_at/received_files，不要求T10未来四步骤或final review完成。正文保留原asof与执行条件，事前核查不保证未来有效。实际输出与公开precheck.json一致，私有host-*原文不进入candidate evidence。失败保存真实记录，禁止进入delivery或包装正常候选；改字节须重新预核对。
6. producer的local_delivery仍经HostRequest，由宿主用现有文件读取与Bash/Python hash工具实际打开所选root内报告、索引、claims、覆盖/资格摘要及所有必要附件，返回逐文件绝对路径、read_at、实际hash/size。不是新投递API，不打开预览，不要求用户技术操作、付款或阅读。全部核对成功才delivered；真实读取/附件/path/hash错误记录delivery_failed、定位失败项/原因的非空真实错误和准确已收子集，不伪造没读文件。
7. local_delivery以真实完成时间核SC09全部required有效界和exclusive/事件条件，记录DeliveryReview；无新检索，真实收到变化仅绑定原事实。正文预先明示来源时点和执行条件，源过期只能按历史/观察呈现。SC09 published_at不回写，SC10不把用户阅读时间当交付。
8. 保存delivery-NN.json及实际事件后才组ReportBundle/CandidateOutput。candidate.datasets含Claims，evidence_refs含公开precheck/delivery及其它需公开的报告对象，不含producer私有host-*原文。源附表准确引用可指原本轮文件；delivered必须实际读所有required文件并在received_files全量准确登记；delivery_failed仅登记准确实际已收子集和真实失败，不把缺收项伪造补齐。
9. validate_sc10独立读取当前完整候选和原允许输入，确定性核全量集合、字段、金额/单位、时间、来源、附件、预核对/交付绑定；report_review用validator角色真实新上下文复核全target/T01—T16，T10此时才核四步骤全量完成，不读producer私有推导。T08以实际delivery.completed_at/DeliveryReview重核全部必要finite/exclusive/event/unknown界；T09对delivered/delivery_failed先独立重核同一完整内容门，再按T12分全实收或真实错误+准确子集核状态。无内容或内容无效不能以delivery_failed免验。只能签ValidationReceipt，不能修改输入或候选求通过。
10. 共享controller按完整规则与use_rules封存；本链只有reporting/evidence_review且16规则全passed才允许。delivery_failed内容真实可封存；最终验证失败不封存但不删真实已交付记录。根随后独立生成GoalEvidence/ResearchResult；SC10不反向引用它们。

宿主实际context/model/tool_call身份取不到时按公共OptionalIdentity unknown并说明，不编造。未调用只保留host_work_required；已实际工具失败按公共failed event/response记录真实原因且消费attempt，不能永远等待。正式四模板本阶段仅登记路径，仍未创建；后续实现必须与冻结Schema一致，不能用测试专用prompt。

同attempt恢复原准确input/method/时钟、合法同role响应及完整工作计划，分片不缩范围。公开副本与成功宿主结构化output规范值相等；最终validator不靠此自证，仍独立核原输入和实际文件。precheck/final review不能共用composition上下文；接收可在自己的新上下文执行实际工具，其已收到事件与内容质量分别表达。原attempt目录、manifest/同级README保留失败、已交付和修正版本；没有新跨轮业务数据库，测试/下载/辅助/原来源捕获全部~/.investment/test/<batch>。

校准顺序依批准brief：规则→根fresh合同审核→validator及独立校准→根冻结→producer→真实四selector→fresh review。本阶段四selector均未执行；每项合法前置和独立预期见method.calibration。上游真实业务依赖尚缺则如实未执行，不造Seal或把静态Schema检查说成完整业务通过。
