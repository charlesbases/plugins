# SC01：内部流程、运行与验证

文档版本：v1。状态：已有确定性实现；自然语言宿主适配尚未实现。

[外部说明](README.md) · [总体架构](../../architecture.md) · [公共契约](../../contracts.md) · [SC01 精确规范](../../spec/v1/sc01.md) · [项目约束](../../../AGENTS.md)

子链标识：SC01。说明目录：docs/chains/account/。代码包：[src/investment/chains/account](../../../src/investment/chains/account/)。精确规范：[SC01 v1](../../spec/v1/sc01.md)。

## 内部流程与实现位置

内部组件仅服务已定义的整笔输入输出，不暴露私有适配约定。当前实现确定性处理JSON，无大模型调用。宿主可适配用户描述，但必须产生相同强格式，不猜交易ID、基金份额类、金额、份额、费用或实际时间。

| 路径 | 实际职责 |
|---|---|
| scripts/run_sc01.py | 正式run或只读prepare；显式控制身份，不从非法JSON猜身份 |
| runtime/controller.py | 根锁、恢复、冻结上下文；每轮先真实流程进程再真实校验进程，失败反馈及三次预算，成功后提交封存 |
| contracts.py | canonical JSON、封闭Schema、精确Decimal和基础引用格式 |
| references.py | URI白名单、实际字节SHA/长度、Dataset元数据和主键；同一只读事务读取两表 |
| account/timing.py | 只读问题、明确答复优先、整笔预计完成状态转换；不自行取系统时间、不写库 |
| scripts/process_sc01.py → account/chain.py | 独立装载输入、归并trade_id、派生持仓/pending/Context和CandidateOutput，只写当前attempt候选 |
| scripts/validate_sc01.py → account/validation.py | 独立进程重新读取冻结输入，独立重建归并/时序/派生输出；不导入chain或timing |
| storage.py | 两个CHECK UDF、真实进程结果许可、完整基线CAS、两表同事务、严格布局检查及finalize |
| runtime/runs.py | 不可变文件、清单与每日README、用途保护、清理意图及中断恢复 |

## 提交、恢复与清理

流程输入不能解析时返回production Failure；有候选后再做最终独立验收；验收失败不提交，事务失败全部回滚。输入、基线、方法及真实执行截止时点在三次尝试中保持不变。重启中断的未提交attempt仍占预算。三次失败返回FailureSummary，禁止后续流程。

业务事实只存必要Trade及当前ledger_meta。运行期Context、持仓、pending、源输入、基线、候选、凭证、封存和系统执行时钟留在runs。generation依据业务事实变化递增；last_commit每次成功更新，finalize不增加generation。自动时间处理不制造新的用户报告时间。

提交前保存绑定候选及凭证；真实验收许可必须属于当前running/commit阶段。交易与finalized=false的marker同事务提交。随后用marker内固定sealed_at完成封存、执行记录、清单、README，再核对绑定并finalize。未finalized不可被新提交覆盖；恢复材料不足明确已经提交，禁止再次记账。

整笔取消/失败不保留部分确认。预计判断可被明确否认撤回，但只撤expected_due。实际用户确认不得由普通更新消除；明确更正绑定旧行SHA。实际确认份额贡献持仓一次；卖出现金到账不再次减份额。没有余额核算、分批数组、尾款分摊或永久source_record历史黑名单。

完成且无有效下游使用的runs可清理，清理不删除业务事实；已清理旧产物不承诺历史引用可读。下一次从真实数据库重新导出新运行结果。精确格式、谓词和边界统一见 [SC01 规范](../../spec/v1/sc01.md)。

不再调度独立input worker。流程读取Schema/引用/参数等基础约束，校验器最终重读全部原始依据、独立执行业务重建并报告28项检查。input_check_ref字段在正式新尝试为NA，不生成input-check.json，也不作为成功条件。反馈仅从前次failed尝试的Failure读取，检查实际哈希、Schema、run/invocation/attempt，不从reason自由文本推导账户事实。

临时文件由`runtime.runs.cleanup`在持有`root_lock`且S01/S02通过时清理；仅有必要交易和当前meta永久存库，不保留历史账户快照，不提供删除交易/旧版本API。已完成且无有效下游使用的调用可清理；未收尾提交必须先恢复。没有按日自动删除任务。根锁为`runs/sc01/.controller.lock`；清理意图为日期目录下`.cleanup-<invocation_id>.json`，恢复沿用同一登记清单。

## 运行与验证

当前入口接收规范化的StageInput JSON，按“流程脚本→独立校验脚本”执行；内部确定性处理，无大模型调用或提示词。自然语言材料转为SourceRecord的入口适配器尚未提供；字段必须满足正式Schema，不能把一句话当作交易数据。首次initialize登记确认无持仓起点；之后refresh使用实际SQLite当前版本和本次变化。

在本插件目录安装依赖（以下为Windows上的Bash示例，已验证Python 3.12；SQLite须支持STRICT表）：

~~~bash
python -m venv .venv
.venv/Scripts/python.exe -B -m pip install --no-cache-dir -r requirements.txt
~~~

正式运行：

~~~bash
.venv/Scripts/python.exe -B scripts/run_sc01.py --input /absolute/path/stage-input.json --run-id run-001 --invocation-id invocation-001
~~~

正式运行根固定为`~/.investment`；单测根固定在`~/.investment/test/<test_run_id>/`内，`--root`须与运行模式匹配。StageInput引用的源文件须已放在该根的`runs/sc01/YYYYMMDD/invocation_id/`中，URI、哈希、大小须与真实字节一致。正式用户提供的`--input`可在外部，但不能来自test目录；单测的`--input`及验证候选入口必须位于本批测试根，不能跨批次或读取正式运行文件；读取前校验，随后冻结为该调用的`input.json`。正常用户源为`user_confirmation`；构造数据使用`synthetic_fixture`并显式加`--fixture-mode`，其结果只能标为`observation_only`，不得进入正式账户库。

进程成功时stdout为RunResult（含SealedOutput引用、attempt_no和allowed_uses）、退出码0；三次失败时为FailureSummary、退出码1；冻结输入变更、终态违规等控制拒绝为ProtocolRejection、退出码2。stderr输出实际校验进程及失败原因。业务候选、校验结果、执行记录均为闭合JSON；README由机器清单确定性重建。

独立校验入口为[scripts/validate_sc01.py](../../../scripts/validate_sc01.py)，使用`--phase input|output`，并指定`--root --input --run-id --invocation-id --attempt-no`；output另指定同次实际`--candidate`。校验器只读，不导入生产者，不写库。

只读准备可选批量问题（源文件须位于该根的合法run目录；不会登记调用或消耗attempt）：

~~~bash
.venv/Scripts/python.exe -B scripts/run_sc01.py --prepare --input /absolute/path/submitted-input.json --run-id run-001 --invocation-id invocation-001
~~~

宿主将答复写为SourceRecord.arrival_replies，重新计算所有受影响引用的哈希/大小，再执行正式run。自然语言问询适配器尚未实现，--prepare输出结构化问题。

完整真实场景单测：

~~~bash
.venv/Scripts/python.exe -B scripts/verify_sc01.py --self-test
~~~

子链验证脚本先校准并固定，再实现流程；单测脚本启动同一CLI、真实流程和独立验证进程。它每次在`~/.investment/test/`下创建空白批次根，首次无持仓，后续步骤重新打开本批次上一轮实际数据库；不读取示例expected输出或以缓存结果代替执行。输出`verification-report.json`及本批次data/runs文件，批次根路径见stdout。构造交易显式标记为synthetic_fixture，无测试专用提示词，不写正式用户账户。

流程脚本成功stdout只返回CandidateOutput的ArtifactRef，失败返回Failure；候选引用TransactionContext及trades、positions、pending_transactions三张业务表。编排入口成功返回RunResult并引用SealedOutput；验证脚本独立输出ValidationReceipt。单测报告中的invocations、checks、stdout/stderr是测试调用记录和检查统计，不是流程业务输出，不能包装成后续正式子链输入。日期manifest的invocations是运行控制索引，也不属于业务表。单测结果保留synthetic_fixture来源及observation_only用途限制；展示子链输出时直接提供对应调用的真实业务文件，单测报告另行提供。

### 场景选择与独立预期

校验失败时，编排器将实际Failure留在本次attempt；下一次流程验证其引用、身份和字段，再从相同冻结输入重新处理。三次全部失败禁止后链并给出原因。流程不能生成候选时返回强格式production Failure，不伪造校验凭证。只有校验通过、账本提交、封存及finalize完成后，输出才能按后续子链契约交接。

单测主线是首次零持仓、再次实际读库、买入、已有持仓后卖出、预计未到/到期、用户提前完成/未完成/延期/未回复、补报及不重复记账。到期用例先登记未来时间，实际等待再执行。每个用例先说明实际入口、合法前置状态、触发条件及独立预期，验证脚本校准也适用此准入条件；不能以反例或故障注入名义豁免可达性，更不能编造业务来增加覆盖率。真实性大于覆盖率，具体开发与审查方法统一见[项目约束](../../../AGENTS.md)。全部数据限定于~/.investment/test/，不读取缓存预期输出，不使用专用提示词。

校验器修改条件、开发顺序、审查方法和场景准入统一遵守 [项目约束](../../../AGENTS.md#6-开发与运行顺序)。独立验证结果与单测报告分别读取，命令退出成功不替代业务资格判断。
