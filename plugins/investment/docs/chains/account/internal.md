# SC01 内部职责

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

流程输入不能解析时返回production Failure；有候选后再做最终独立验收；验收失败不提交，事务失败全部回滚。输入、基线、方法及真实执行截止时点在三次尝试中保持不变。重启中断的未提交attempt仍占预算。三次失败返回FailureSummary，禁止后续流程。

业务事实只存必要Trade及当前ledger_meta。运行期Context、持仓、pending、源输入、基线、候选、凭证、封存和系统执行时钟留在runs。generation依据业务事实变化递增；last_commit每次成功更新，finalize不增加generation。自动时间处理不制造新的用户报告时间。

提交前保存绑定候选及凭证；真实验收许可必须属于当前running/commit阶段。交易与finalized=false的marker同事务提交。随后用marker内固定sealed_at完成封存、执行记录、清单、README，再核对绑定并finalize。未finalized不可被新提交覆盖；恢复材料不足明确已经提交，禁止再次记账。

整笔取消/失败不保留部分确认。预计判断可被明确否认撤回，但只撤expected_due。实际用户确认不得由普通更新消除；明确更正绑定旧行SHA。实际确认份额贡献持仓一次；卖出现金到账不再次减份额。没有余额核算、分批数组、尾款分摊或永久source_record历史黑名单。

完成且无有效下游使用的runs可清理，清理不删除业务事实；已清理旧产物不承诺历史引用可读。下一次从真实数据库重新导出新运行结果。精确格式、谓词和边界统一见[规范](../../spec/v1/README.md)。

不再调度独立input worker。流程读取Schema/引用/参数等基础约束，校验器最终重读全部原始依据、独立执行业务重建并报告28项检查。input_check_ref字段在正式新尝试为NA，不生成input-check.json，也不作为成功条件。反馈仅从前次failed尝试的Failure读取，检查实际哈希、Schema、run/invocation/attempt，不从reason自由文本推导账户事实。
