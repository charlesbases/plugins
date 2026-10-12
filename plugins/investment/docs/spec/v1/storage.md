# SC01 必要数据存储

正式数据库固定为~/.investment/data/investment.sqlite；单测数据库为~/.investment/test/<test_run_id>/data/investment.sqlite，两者不能混写。仅两张STRICT表，DDL见[storage.sql](storage.sql)，嵌套对象见[storage.schema.json](storage.schema.json)。现有不匹配布局明确拒绝，不清库、不迁移、不伪装成首次账户。

先解析实际根并校验运行模式，再创建目录、根锁和SQLite连接。synthetic_fixture仅允许测试根，user_confirmation仅允许正式根；接受许可、提交及finalize均检查来源与根绑定。--root不能将模拟账本指向正式路径。单测所有输入、运行文件、报告和辅助材料同处本批test根，不使用系统临时目录。

| 表及主键 | SQL列 | 内容/生命周期 |
|---|---|---|
| trades(account_id,trade_id) | account_id TEXT、trade_id TEXT、trade_json TEXT；均NOT NULL | 一笔canonical Trade；精确字段以investment.trade为准；同ID更新当前事实，不复制整账户快照 |
| ledger_meta(account_id) | account_id TEXT、meta_json TEXT；均NOT NULL | 每账户一行canonical LedgerMeta；只保存当前控制标记，不存历史版本 |

TradeRecord外层和trade_json内层同时验证；内层trade_id必须等于SQL主键，meta.account_id必须等于SQL主键。trades_instrument索引为(account_id,json_extract(trade_json,'$.instrument_id'))。金额和份额始终是JSON中的十进制字符串，不使用SQLite REAL或SUM浮点求和。交易稳定ID长期承担去重，不建立永久源record_id去重表。

LedgerMeta精确字段：account_id、platform、currency=CNY、opening、source_authority、generation、ledger_digest、known_as_of、transaction_as_of、last_commit。opening包含start_at、positions=confirmed_empty、coverage=recorded_transactions_since_start；来源只能user_confirmation或synthetic_fixture且不能互相升格。

last_commit精确字段：run_id、invocation_id、execution_date、attempt_no(1..3)、input_sha256、candidate_sha256、receipt_sha256、sealed_at、finalized。SHA为64位小写十六进制。该对象只说明最近成功提交的控制身份；它不是银行实际到账时间。

ledger_digest = SHA256(canonical({scope,trades}))。scope恰含account_id、platform、currency、opening、source_authority、known_as_of、transaction_as_of；trades={rows:[按trade_id排序的完整Trade]}。摘要不包含generation、last_commit或派生输出。首次generation=1；必要交易事实状态变化才加1，相同业务事实的新执行只更新marker，finalize同样不增业务generation。

两个UDF负责写边界：investment_trade_v1验证外层、canonical内层及当前真实独立验收许可中的准确行；investment_meta_v1验证完整meta及该许可的准确JSON。函数未注册、无许可、抛错或非1一律写失败。禁止业务DELETE；没有旧版本删除API。许可来自控制器实际Popen验证进程接收的字节及当前running/commit尝试，不来自JSON声明或缓存凭证。

事务流程：独立流程脚本读取冻结输入并生成候选 → 独立校验脚本核对全部输入依据及输出 → 保存准确候选/凭证 → 真实进程绑定许可 → BEGIN IMMEDIATE → 当前两表完整基线与冻结generation/digest/marker CAS → 必要交易upsert及finalized=false marker一起提交。中途任何行失败全部回滚。只读读取两表使用一个SQLite读事务，防止并发时混合两个版本。

完成运行产物后，用相同marker的sealed_at封存，写执行记录/清单/日期README，再核对输入、候选、凭证、封存与业务摘要，CAS置finalized=true。未finalized不得被下一次提交覆盖。重启优先恢复其绑定运行；材料缺失或不符明确已经提交、勿重复提交，不继续新attempt。

源记录、冻结基线、方法、Context、持仓、pending、候选、凭证、封存、错误和执行记录均位于runs，不入库。Context的引用绑定本轮准确文件；没有历史investment-data URI。已清理旧运行不保证旧结果回放，下一次重新从当前真实两表导出；不能用最新账本冒充旧字节。

数据增长与必要性：交易表按实际买卖笔数增长，每笔只有当前必要事实；meta按账户数增长，没有逐日整账户副本。日内产物按实际用途结束清理，未完成尝试、未finalized提交或有效下游引用阻止删除。完成的当前marker不会永久阻止清理。清理仅删除该调用已登记文件，保留日期manifest和README的已清理说明，不删除数据库事实；不安排自动清理任务。
