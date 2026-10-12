# v1 强校验规范

SC01采用整笔买卖记录。公共StageInput → CandidateOutput → ValidationReceipt → SealedOutput外壳不变；业务载荷为TransactionContext。字段必须满足封闭Schema和独立确定性语义校验，reason仅展示，不驱动业务分支。

| 文件 | 权威内容 |
|---|---|
| [common.schema.json](common.schema.json) | 公共外壳、引用、精确值、Gap |
| [sc01.schema.json](sc01.schema.json) | 整笔输入、阶段、交易、持仓、pending、Context及只读问题 |
| [storage.schema.json](storage.schema.json)、[storage.sql](storage.sql) | 两表外层、LedgerMeta、冻结基线与运行暂存对象 |
| [runs.schema.json](runs.schema.json) | 三次尝试、失败、清单、清理及成功RunResult |
| [registry.json](registry.json)、[method.json](method.json) | 注册类型、表主键/单位/空值、固定方法及record_unit=whole_trade |
| [fields.md](fields.md) | 全字段类型/约束索引及必要性映射 |
| [rules.md](rules.md) | 确定性业务谓词与失败控制 |
| [storage.md](storage.md) | 两表事务、恢复、增长和清理边界 |
| [examples](examples/README.md) | 强格式构造输入示例；不提供缓存预期输出 |

SQLite仅必要交易及当前账本元数据；派生输出在runs。输入失败不生产，输出失败不提交；首次加两次重试，共三次。提交已成功而产物未收尾时先恢复，不能重复提交。到账处理保留真实user_reported与expected_due的不同依据，实际金额、份额、时间缺失不填0或系统时间。

唯一完整场景单测入口为scripts/verify_sc01.py --self-test，在~/.investment/test/<test_run_id>/创建空白隔离根，调用实际流程、独立验证进程和本批SQLite；模拟数据不得进入正式data/runs或系统临时根。--root须与模式及来源匹配，强路径校验见[规则](rules.md)。verification-report.json中的invocations/checks仅为单测报告，流程业务输出与校验凭证分别按已有Schema产生，不将单测报告作为正式后链输入。其他子链只有职责设计，registry当前只启用SC01。安装、运行及只读准备见[插件README](../../../README.md#5-sc01运行与验证)。

SC01执行顺序固定为process_sc01流程候选→validate_sc01最终验收→原子提交/封存/交接。最终凭证完整列J/R/I/A/V/U共28项。verify_sc01是模拟真实输入的单测入口；验证标准先校准固定，流程代码随后实现，不使用失败输出缓存，也不为通过测试放宽标准。精确反馈及三次重处理协议见rules.md。
