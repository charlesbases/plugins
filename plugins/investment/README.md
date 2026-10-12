# Investment：业务子链与强校验输入输出设计

插件版本：0.0.1。文档版本：v1。状态：SC01已有可运行实现；其他子链为设计规范。

本设计从投资研究目标、金融核算与统计验证要求推导。investment.bak 仅提供业务链路与理论材料的参考；旧版实现、函数划分、字段和运行结果均不作为本设计依据。SC01 的字段、JSON Schema、数据库格式及三次尝试约束见 [v1 格式规范](docs/spec/v1/README.md)，运行方式见本页第5节。其他子链仍需先补齐业务 Schema 与确定性规则再启用。

## 1. 目标、总输入与总输出

研究目标：配置来源内的近期国内外信息 → 可核事实与行业机制 → 搜索同类完整名单 → 逐基金金融取材 → 预测与资金方案比较 → 当前持仓及候选基金的加、减、保持建议 → 可追溯报告。

研究渠道默认面向天天基金，核算币种为 CNY。渠道与币种必须在本次任务中显式登记；研究资料可以具有其他原始币种，转换时必须保留真实汇率、报价方向、可得时点与来源。

总输入：

| 输入 | 内容 | 确认责任 |
|---|---|---|
| TransactionFacts + Changes | 基金买卖、确认份额、交易及到账状态 | 用户确认；交SC01登记 |
| FundingPlan | 本次分析使用的可投资金额与资金计划 | 独立任务输入，不由SC01计算或维护余额 |
| UserMandate | 投资期限、流动性需求、损失口径、风险容忍及额外约束 | 用户确认；不制造偏好 |
| ResearchScope | 信息窗口、来源域、渠道、产品域及范围构建规则 | 研究方法登记，实际覆盖如实记录 |
| MethodPolicy | 数据、模型、基准、比较目标、统计验证及参数确定规则 | 研究方法事前登记；独立验证前冻结 |
| DecisionClock | 信息截止时间、决策时间、允许的发布时效 | 调度者记录真实时钟 |

用户只需确认实际账户事实和影响决策的偏好。内部检索计划、数据 schema、模型参数和实验协议由研究流程生成并审计，不要求用户填写。

总输出为 ResearchResult，引用独立根目标验证者生成的 GoalEvidence，以及实际取得并封存的 SC10 ReportBundle。goal_status 分为 achieved、unmet、pending；报告交付状态仍由 ReportBundle.delivery_status 单独表达。完整研究要求原同类列表闭合、全体成员及实际风险有处置、所有应比较成员真实参与比较、科学与金融资格以及报告内容核对通过。仅生成报告或处理结束不能证明目标达成；未来真实投资结果另行记录。

新闻来源可以配置站点或来源域，不要求全球采集。基金比较先主动搜索声明渠道和产品域内的同类完整列表，不能以手选基金、top N、成功下载集合或模型就绪集合定义分母。有限计算保留原名单、未知和未比较项，只能形成相应范围的观察结果；不能据此降低完整研究目标。

必要用户事实尚待确认或本次可继续的内部工作形成 pending；尚无有效报告时也返回根证据和总结果，未取得的账户/报告引用以 unknown 表达并保留控制记录；本次完整目标已判定不能完成、必要证据不可取得或仅能等待未来成熟时为 unmet。金额资格不足时仍可交付来源清楚的观察报告并明确缺口，报告不预判根目标。具体对象与必要门槛见 [公共契约](docs/contracts.md)。

当前动作是建议，不是平台订单或成交。用户实际执行后的确认、份额和到账事实进入下一次交易上下文，形成新的研究输入。

## 2. 子链划分及理由

划分依据是不同的信息责任、变更原因、数据形态与验收标准。十条子链是本设计的工程选择，不是理论规定的唯一数量。边界应允许更换采集器、模型或求解器，同时保持对下游的业务含义。

| ID | 子链 | 主要解决的问题 | 核心输出 | 划分理由 |
|---|---|---|---|---|
| SC01 | 基金买卖交易记录 | 买卖了什么、确认多少份额、是否到账 | TransactionContext | 真实交易登记与后续估值、资金及风险分析分开 |
| SC02 | 信息发现与原文采集 | 获得了哪些材料、版本与未覆盖项 | EvidenceCorpus | 获取材料与评价内容具有不同验收标准 |
| SC03 | 事实评审与经济假说 | 来源支持什么事实、可能经什么机制影响行业 | ResearchAssessment | 事实、机制假说和收益预测分开 |
| SC04 | 投资工具范围与暴露映射 | 主动搜索完整同类名单、核验身份暴露并保留风险 | InstrumentUniverse | 范围与身份先确定，避免以结果选择分母 |
| SC05 | 时点数据与实验协议 | 主动取得金融资料、审计时点可得性并冻结实验 | DatasetBundle、ExperimentProtocol | 版本与时间对齐不承担拟合和预测 |
| SC06 | 预测与联合情景 | 当前信息条件下的收益、价格及风险不确定性如何表示 | ForecastBundle | 预测方法不自行决定资金金额或授予资格 |
| SC07 | 组合与资金方案选择 | 在真实金融约束下应选什么规则与当前动作 | FrozenPolicy | 目标、资金和费用约束独立于预测方法 |
| SC08 | 独立资格验证 | 冻结规则是否有足够预测、经济和金融证据 | QualificationVerdict | 选择与评价隔离，避免循环证明赢家 |
| SC09 | 建议形成与发布许可 | 已验证动作在发布时是否仍适用、用户如何执行 | RecommendationSet | 保持经济决策含义，单独核对时效与执行条件 |
| SC10 | 报告交付 | 准确说明完整名单、实际比较及限制，记录交付 | ReportBundle | 报告只解释封存结果，不重新选策略或改金额 |

## 3. 文档导航

| 文档 | 权威内容 |
|---|---|
| [数据与运行目录约定](#4-数据与运行目录约定) | data/runs 分工、每日 README、调用内容登记及必要数据保留 |
| [总体设计](docs/architecture.md) | 总链路、模块职责、内部扩展与并发、参数和证据边界 |
| [公共契约](docs/contracts.md) | 数据格式、通用类型、交接对象、强校验与用途许可 |

| 子链 | 外部契约 | 内部流程 |
|---|---|---|
| SC01 基金买卖交易记录 | [README](docs/chains/account/README.md) | [internal](docs/chains/account/internal.md) |
| SC02 信息发现与原文采集 | [README](docs/chains/news_collection/README.md) | [internal](docs/chains/news_collection/internal.md) |
| SC03 事实评审与经济假说 | [README](docs/chains/news_assessment/README.md) | [internal](docs/chains/news_assessment/internal.md) |
| SC04 投资工具范围与暴露映射 | [README](docs/chains/fund_universe/README.md) | [internal](docs/chains/fund_universe/internal.md) |
| SC05 时点数据与实验协议 | [README](docs/chains/research_dataset/README.md) | [internal](docs/chains/research_dataset/internal.md) |
| SC06 预测与联合情景 | [README](docs/chains/forecast/README.md) | [internal](docs/chains/forecast/internal.md) |
| SC07 组合与资金方案选择 | [README](docs/chains/portfolio_selection/README.md) | [internal](docs/chains/portfolio_selection/internal.md) |
| SC08 独立资格验证 | [README](docs/chains/qualification/README.md) | [internal](docs/chains/qualification/internal.md) |
| SC09 建议形成与发布许可 | [README](docs/chains/recommendation/README.md) | [internal](docs/chains/recommendation/internal.md) |
| SC10 报告交付 | [README](docs/chains/reporting/README.md) | [internal](docs/chains/reporting/internal.md) |

文档按业务责任维护：总览负责目标和导航，公共规则与架构集中定义，子链外部契约和内部流程分别记录。SC01的运行代码、独立验证器与存储按正式规范实现；其余子链尚未实现。

## 4. 数据与运行目录约定

本页规定运行数据放在哪里、每日 README 怎样说明各次调用，以及哪些数据需要保留。SC01控制器创建目录并维护说明；清理接口只接受已满足S01/S02的调用，不按日期自动清空。

### 4.1. 根目录结构

~~~text
~/.investment/
├── data/
│   └── investment.sqlite              # 本地 SQLite 方案的文件示例
├── runs/
│   └── <子链标识>/
│       └── <执行日期YYYYMMDD>/
│           ├── manifest.json          # 当天调用与尝试状态的机器记录
│           ├── README.md              # 从 manifest 生成各次调用内容说明
│           ├── <invocation_id_1>/
│           └── <invocation_id_2>/
└── test/
    └── <test_run_id>/                  # 单测批次，不能作为正式运行根
        ├── data/investment.sqlite     # 本批模拟交易数据库
        ├── runs/sc01/<YYYYMMDD>/...    # 本批输入、产物及校验结果
        └── verification-report.json   # 完整场景单测的汇总报告
~~~

- data 保存每天确有后续用途的必要业务数据。SC01 本地账户存储采用 SQLite，表结构和完整校验约束见 [数据库规范](docs/spec/v1/storage.md)；其他子链数据尚不预建表。
- runs 保存本轮输入副本、中间结果、临时校验材料、日志和本次输出；按用途结束情况清理，不默认永久保存或逐份写入数据库。
- 子链标识用于区分业务子链，例如 sc01；invocation_id 对应公共 StageInput 的单次调用身份。不同调用使用不同目录，不覆盖其他调用的文件。
- YYYYMMDD 是执行日期，例如 20261014；使用统一登记的时区，当前为 Asia/Shanghai。同次执行跨日继续时沿用原执行目录。
- 账户事实时点、成交日、确认日和净值日期另行记录，不能从目录日期推断。

test_run_id是单测批次标识；该批次内可以有多个invocation_id，每次调用再有最多三个attempt。所有单测输入、独立数据库、运行产物、报告和辅助文件必须处于~/.investment/test/内，不能使用AppData或系统默认临时目录，不能进入正式data/runs。单测复用实际流程和验证代码，使用自己的根与历史账本，不连接正式账户库。

路径采用强校验，令P=resolve(~/.investment)、T=P/test、R=resolve(--root)：

| 运行类型 | 根路径条件 | 输入来源 |
|---|---|---|
| 正式运行，不带--fixture-mode | R=P | user_confirmation |
| 单测，带--fixture-mode | R是T的严格后代，R不能等于T | synthetic_fixture |

单测批次根由脚本在T下新建独立目录；解析实际路径，不能用路径穿越或符号链接将测试根指向正式根。根及模式在读取输入、创建锁、运行目录或打开数据库之前检查；流程写候选前、数据库接受许可/提交及finalize时还须核对来源与根类型。错误根作为启动拒绝返回退出码2，不生成伪造业务输出或校验凭证。--root仅用于传递合法根，不能绕过这些条件。

单次调用内按 attempt-01、attempt-02、attempt-03 隔离尝试，首次加两次重试最多三次；只创建实际发生的尝试。SC01 文件、manifest 字段和路径约束见 [规则与流程](docs/spec/v1/rules.md)。内部目录不改变子链外部输入输出。

### 4.2. 每日 README 的位置与职责

每个子链的日期目录必须有：

~~~text
~/.investment/runs/<子链标识>/<执行日期YYYYMMDD>/README.md
~~~

该 README 与 invocation_id 目录同级，用于让读者知道当天每次调用处理什么、目录里的主要文件是什么以及是否还需要保留。

每日 README 按同目录 manifest.json 的固定字段与模板生成，逐项说明：

| 内容 | 要求 |
|---|---|
| invocation_id | 与对应执行目录、实际调用身份一致，每次调用一条记录 |
| 执行内容 | 本次处理的对象或范围、目的及内容摘要，能区分同日不同调用 |
| 主要文件及用途 | 已生成文件相对于日期目录的位置，以及各文件保存的内容；未生成时如实说明 |
| 执行状态 | 依据实际 ExecutionRecord 填写 pending、running、completed 或 failed；失败时附简短原因 |
| 保留与清理条件 | 哪些内容本轮用完可清理，哪些仍有后续用途，以及相应结束条件 |

说明只记录概要、位置和用途，完整业务数据留在对应文件或必要数据库记录中。它是人工阅读的说明，不替代 JSON 交接包或独立验收；completed 只表示处理结束，不表示业务资格通过。每日 README 不另行落库，也不成为新的永久运行历史。

### 4.3. 每日 README 内容模板

下面是填写模板，所有占位内容必须由真实调用情况替换，不能作为已经发生的执行记录。

~~~markdown
# <子链标识>：<YYYYMMDD> 执行说明

执行日期：<YYYYMMDD>
时区：<本次登记时区>

| invocation_id | 执行内容 | 主要文件及用途 | 执行状态 | 保留与清理条件 |
|---|---|---|---|---|
| <调用标识> | <处理对象/范围、目的和内容摘要> | <调用标识>/<实际文件名>：<内容及用途；未生成则说明> | <实际状态；失败原因如有> | <仍需使用的内容及用途结束条件> |
~~~

实际文件位置使用相对于当前日期目录的路径。无需统一规定所有子链都必须生成相同文件；README 必须解释实际存在的主要内容。

### 4.4. 登记与更新约束

1. 创建某次调用的执行目录时，在当天 README 登记它的身份、内容和实际状态。
2. 执行结束或发生失败后，更新同一条记录，补充实际文件、结果概要、失败原因及保留条件。
3. 同日多次调用逐项登记；更新本次记录不能覆盖或丢失其他调用的说明。
4. 尚未执行或尚未完成的内容不得写成已完成；文件尚未生成时不得写成已经存在。
5. 说明文件可随真实执行情况更新，其状态不作为下游取得业务许可的依据。

### 4.5. 必要数据与清理约束

持久化以明确的后续用途为依据，不因为某项数据曾经使用过就永久保存。

- SC01的data只保留逐笔必要买卖事实和一行当前账本元数据，登记持仓与未完成清单在runs派生；不记录余额、本金或资金来源。其他子链仅保存已明确需要跨日使用的业务数据。
- 临时输入副本、解析文本、转换结果和核对中间表等，在本轮处理与必要验收结束且没有后续依赖时可以清理。
- 未完成任务、跨日订单核对或其他有效用途仍需要的内容，保留到用途结束，不按日期直接清空。
- 清理前完成必要业务数据的持久化，并确认持续使用的数据没有仅指向待删除文件的引用；不得用新文件冒充旧版本。
- 清理调用目录时，同步移除每日 README 中对应条目，或明确标为已清理；不能留下看似仍可读取的有效文件位置。
- SC01清理保留日期manifest与README的调用状态及已清理说明，不删除日期索引；调用内已登记临时文件可在条件满足后删除。

过期材料清理后，不再承诺能够完整重放依赖这些材料的历史过程。当前有效用途所需的准确引用仍须可读。

### 4.6. 本阶段范围

SC01实现目录、机器格式、每日说明、独立验收、SQLite原子提交和保留/清理接口。校验不通过重试两次，连同首次最多三次；三次全部失败即禁止交给后续子链，由控制出口给出每次失败的规则编号、字段定位和原因。完整状态约束见 [v1 规则](docs/spec/v1/rules.md)。SC02—SC10尚未实现；存储内部机制不增加子链的业务输入输出字段。

## 5. SC01运行与验证

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

独立校验入口为[scripts/validate_sc01.py](scripts/validate_sc01.py)，使用`--phase input|output`，并指定`--root --input --run-id --invocation-id --attempt-no`；output另指定同次实际`--candidate`。校验器只读，不导入生产者，不写库。

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

临时文件由`runtime.runs.cleanup`在持有`root_lock`且S01/S02通过时清理；仅有必要交易和当前meta永久存库，不保留历史账户快照，不提供删除交易/旧版本API。已完成且无有效下游使用的调用可清理；未收尾提交必须先恢复。没有按日自动删除任务。根锁为`runs/sc01/.controller.lock`；清理意图为日期目录下`.cleanup-<invocation_id>.json`，恢复沿用同一登记清单。

## SC01 预计时间处理

强格式见[Schema](docs/spec/v1/sc01.schema.json)、[字段表](docs/spec/v1/fields.md)及[确定性规则](docs/spec/v1/rules.md)。一笔交易整笔确认/到账，确认与到账各一个状态对象，不提供部分成交、分次到账及剩余尾款。先处理明确回复及延期，再按invocation.created_at判断预计时间；三次尝试共用此时点。实际时间、份额和金额缺失时保留unknown。

--prepare只读返回可选问题，答复收齐后执行正式run。SQLite仅两表：trades逐笔交易；ledger_meta当前generation/digest、登记范围、真实用户时点及last_commit。源输入、冻结基线、Context、持仓、pending和验收产物在runs。到账只改变卖出款项状态，不再次扣份额或生成余额。每次调用的内容、文件及状态由日期README确定性说明。

唯一完整场景单测入口为scripts/verify_sc01.py；它直接运行同一CLI和独立进程，随后从本批次实际SQLite核对结果。已有不匹配格式的数据库明确拒绝，不静默迁移、清理或重建；单测始终新建~/.investment/test/内的隔离根。当前自然语言转强格式的宿主适配尚未实现。

### SC01脚本职责与单测

| 入口 | 职责 |
|---|---|
| scripts/run_sc01.py | 编排：冻结输入、实际账本基线、方法和执行时点；控制最多三次尝试，成功后提交封存 |
| scripts/process_sc01.py | 流程：独立装载输入和基线，处理整笔交易并写本次attempt候选；不调用校验器业务算法或写业务库 |
| scripts/validate_sc01.py --phase output | 校验：候选产生后，独立读取输入/基线/候选，核对格式、来源、状态及计算，生成完整28项凭证 |
| scripts/verify_sc01.py --self-test | 单测：构造真实场景输入，调用实际流程和验证脚本，再核对子链输出与本批SQLite |

校验失败时，编排器将实际Failure留在本次attempt；下一次流程验证其引用、身份和字段，再从相同冻结输入重新处理。三次全部失败禁止后链并给出原因。流程不能生成候选时返回强格式production Failure，不伪造校验凭证。只有校验通过、账本提交、封存及finalize完成后，输出才能按后续子链契约交接。

单测主线是首次零持仓、再次实际读库、买入、已有持仓后卖出、预计未到/到期、用户提前完成/未完成/延期/未回复、补报及不重复记账。到期用例先登记未来时间，实际等待再执行。验证脚本另用独立预期校准：正确500−200=300通过，错误格式、301份持仓、无输入依据的501份确认被拒绝。错误候选和故障注入是校验/回退控制测试，不作为额外基金业务。全部数据限定于~/.investment/test/，不读取缓存预期输出，不使用专用提示词。

修改校验器须先证明其漏洞或规则与需求矛盾；流程未通过时先核对真实输入和校验依据，修复流程后重跑，不能修改校验标准迎合输出。
