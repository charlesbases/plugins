# v1 机器规范索引

本目录负责闭合 Schema、精确关系规则、SQL、类型登记和方法定义。各子链的职责与模型见外部说明，内部编排、安装、命令和场景验证见 internal；本索引不记录单次执行结果。

## 公共与派生参考

| 文件 | 权威范围 |
|---|---|
| [common.schema.json](common.schema.json) | StageInput、CandidateOutput、ValidationReceipt、SealedOutput、引用、精确值与 Gap 的结构 |
| [公共契约](../../contracts.md) | 公共交接、状态和用途语义；具体业务载荷按所属子链规范解释 |
| [fields.md](fields.md) | 公共及 SC01 Schema 的派生查询索引，不作为另一套约束；SC02 字段直接查其 Schema |
| [examples](examples/README.md) | SC01 强格式输入构造示例，保留模拟来源；不提供实际执行结果或缓存预期输出 |

## SC01：交易记录

| 文件 | 权威范围 |
|---|---|
| [sc01.schema.json](sc01.schema.json) | 整笔交易、阶段、持仓、pending、Context、回复及只读问题的结构 |
| [storage.schema.json](storage.schema.json)、[storage.sql](storage.sql) | 两表外层、LedgerMeta、冻结基线、运行暂存对象及数据库约束 |
| [runs.schema.json](runs.schema.json) | SC01 尝试、失败、清单、清理和运行结果的结构 |
| [registry.json](registry.json)、[method.json](method.json) | 公共/SC01 注册类型、表主键/单位/空值、固定方法及 record_unit=whole_trade |
| [sc01.md](sc01.md) | 确定性业务谓词、执行及反馈关系、隔离、事务、恢复、增长和清理约束 |

[外部说明](../../chains/account/README.md) · [内部流程、运行与验证](../../chains/account/internal.md)

## SC02：信息发现与原文采集

| 文件 | 权威范围 |
|---|---|
| [sc02.schema.json](sc02.schema.json) | SC02 输入、Collector 协议、捕获、正文/原生记录、六表、运行及单测对象结构 |
| [registry-sc02.json](registry-sc02.json)、[method-sc02.json](method-sc02.json) | SC02 独立类型登记、数据表元信息和固定方法 |
| [sc02.md](sc02.md) | N01—N12、时间窗口、机会和缺口、独立验收、隔离及三次尝试的确定性约束 |

[外部说明](../../chains/news_collection/README.md) · [内部流程、运行与验证](../../chains/news_collection/internal.md)

SC03—SC10 尚未定义本链专用机器格式与验证规则，不能仅凭职责文档启用。JSON Schema 检查结构；跨对象、状态和事实依据按相应精确规则独立核验，二者不能相互替代。
