# 数据根目录、启动模式与分片

## 根目录和计划

默认使用 `$HOME/.investment/`，与插件源码、安装版本缓存分开。依次使用HOME、USERPROFILE或Python识别的当前用户主目录；Git Bash的`/c/Users/...`在Windows上转换为`C:/Users/...`。无法得到绝对主目录时报告缺口。用户明确指定其他数据目录时，所有status、append、读取和状态写入都使用该目录：CLI传`--root`，Python调用传同一root；不把工作目录当作数据目录。

`config.json` 只保存schema版本、活动计划标识与存储参数，不累加历史记录。计划标识采用不含路径分隔符的稳定名称。多个计划没有明确活动计划时请求选择，不混用账户。

```text
$HOME/.investment/
  config.json
  plans/<plan-id>/
    profile.json
    state/current.json
    indexes/latest.json
    ledger/YYYY/MM/part-000001.jsonl
    snapshots/YYYY/MM/DD/<run-id>.json
    news/YYYY/MM/DD/part-000001.jsonl
    decisions/YYYY/MM/DD/<run-id>.json
    reports/YYYY/MM/DD/<run-id>.md
    market/<fund-code>/YYYY/MM/part-000001.jsonl
    research/YYYY/MM/DD/<run-id>/
    summaries/YYYY/MM/summary.json
```

profile保存约束和背景，current保存最新可用账户状态与已处理账户事件游标，latest保存最近报告及相关分区指针；这些可重建的小文件不是全量历史。账户未初始化时可不创建current。研究资料及原始下载快照存research，标明研究用途，不生成交易。新闻来源状态与news_collection指针按 [news-research.md](news-research.md#采集记录与写入顺序) 保存，不能混入账户游标或覆盖training_lifecycle；更新latest保留其他已有字段。

## 启动判断优先级

1. 用户最新明确持仓说明优先；即使本地没有数据，也按其提供的持仓分析，缺字段时补充，不套用首次分配。
2. 有有效current账户状态或账户流水时，进入持仓复核；只有待确认订单时也需核对占用，不重复建议首次买入。
3. 没有有效账户记录且没有额外持仓说明，进入首次理财：复用已给本金或集中询问必要输入，输出首次购买分配表，不捏造已有仓位。
4. 目录/文件不可读、JSON损坏、记录格式不明或索引指向缺失账户时，报告数据问题，不将故障解释为空账户，不删除历史后重新初始化。

只有新闻、净值、研究模型、profile背景或历史建议，不构成已有持仓。合法的已初始化空持仓状态可能表示清仓，也不是无历史。多计划需要选择时不擅自取第一个。

先运行存储脚本的只读status，再读取current和相应增量；status是模式检查，不代替完整账务校验或金融账户同步。

## 时间分区和大小

- 交易、订单、现金流和更正按有效日期月份写ledger；新闻按日期，净值按基金及月份，决策、报告和快照按日期及唯一run-id。
- 默认JSONL单片上限16 MiB，追加前计算UTF-8字节数，超限写下一编号分片，不拆开一条JSON记录。上限是可配置的工程参数，不是金融参数。
- 单条超过分片上限时拒绝追加，将大型载荷保存为单独原始附件并在事件中保留路径、校验值和摘要。附件本身大时按可复算逻辑拆分，不能把整段历史伪装成一条事件绕开限制。
- current、profile、config及latest等小型JSON默认最多1 MiB；超限拆分明细并保留引用，不无限扩大摘要或索引。
- 单日报告过大时拆成主报告与明细附件。逐日逐月分区降低单文件增长，历史总量仍会增加；不承诺磁盘空间永久足够。

## 增量读取

读取config、profile、current及latest后，只取当前账户所需的未处理流水、近期新闻分区及对应基金的指标窗口。需要长历史指标时，用计算工具流式读取相关月份，给模型摘要和来源，避免把所有原始行塞进上下文。

资料保留与摘要频率统一见 [reporting-and-records.md](reporting-and-records.md#存储与保留)。近期检索窗口不触发历史删除；重要证据仍按决策引用读取。

## 写入、一致性与恢复

事件有稳定event_id，建议包含计划、记录类型、有效日期及来源标识。相同事件必须进入同一规范分区。storage.py对同分区同event_id做幂等检查：内容一致跳过，不同内容拒绝；更正使用新的event_id和supersedes。跨分区去重仍由调用者遵守规范日期并核查，不宣称全库自动去重。

账户记录先确认事件并追加流水，再推导状态，最后更新快照、current及相关latest指针；新闻采用新闻规范的证据、检查清单、来源状态和指针写入顺序，不改变账户。小型JSON通过同目录临时文件、同步和原子替换写入，不能在只写了一半时推进游标。脚本不包含财务状态归约器或新闻采集状态处理器；宿主必须按相应规则核对并更新状态。

组合候选、proposed_contribution、目标仓位、资金腿、同预算排名、跨预算前沿及模拟订单属于research/决策资料，不写成真实ledger事件或修改current。仅确认实际入金后追加cashflow；模拟账户与真实账户永久分开。新的allocation研究按日期/run-id保存并按决策日期分片，复核从源码重算，禁止编辑缓存产物制造通过。

追加使用分区写锁，其他写入者遇锁报错，不同时写同一分片；残留锁需核实进程和记录后人工恢复，不盲目清除。中途失败可能已保存部分完整事件，重试前检查分片完整性；不得将部分写入报告为成功。

对旧格式先读取schema和校验，迁移写入新位置并保留来源映射、行数及校验信息。迁移研究净值不会迁移出真实持仓。重跑同一迁移不重复追加。未经用户指示不删除原文件、不静默清理或压缩数据；归档及备份记录范围、结果和恢复检查。

## 脚本接口

Python 3.10+，仅标准库；使用实际可用Python路径。`--root`缺省按上述主目录规则解析。status的`--plan`可省略：读取config活动计划，或在只有一个计划时选择它；多个计划无活动项则返回needs_plan_selection。append必须显式给出`--plan`、`--collection`和`--partition`，不从config补齐。

```bash
python <skill-dir>/scripts/storage.py status
python <skill-dir>/scripts/storage.py status --plan <plan-id> --explicit-holdings
python <skill-dir>/scripts/storage.py append --plan <plan-id> --collection ledger --partition 2026/10 < records.json
python <skill-dir>/scripts/storage.py append --plan <plan-id> --collection market --partition 007339/2026/09 < nav-records.json
```

append输入是含event_id的JSON对象数组；ledger还需要合法record_type和status。`--max-bytes`可调整分片限制。status不创建目录，append只写指定分区；没有定时任务或账户交易副作用。

所有新闻、报告、账户和模型索引写入者须调用storage.update_latest或update-index，共用计划的索引锁，只提交本次负责的字段。write_small_json拒绝直接写indexes/latest.json。构造依赖旧索引的复合字段时传入旧索引SHA-256；锁内发现旧值已变化就拒绝，不能覆盖另一个写入者的新值。

update-index必须显式给出plan，stdin是本次更新字段的JSON对象。例：`python <skill-dir>/scripts/storage.py update-index --root <root> --plan <plan-id> --expected-index-sha256 <sha256> < changes.json`。首次预期无索引用absent；独立字段合并可不指定条件。共享锁已存在时返回错误，不删除其他任务的锁。
