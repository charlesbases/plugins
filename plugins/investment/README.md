# Investment

插件版本：0.0.1。文档版本：v1。

Investment 将投资研究划分为十条职责明确的子链，通过强格式输入输出和独立验证交接。本页是项目目标与文档入口；开发、验证和审查先遵守 [AGENTS.md](AGENTS.md)。

当前能力：SC01 已有确定性交易登记、独立验证及运行入口，自然语言转强格式的宿主适配尚未实现；SC02 已有可插拔采集框架、独立验收及运行入口，来源适配和整窗覆盖仍有缺口，详见 [SC02 能力边界](docs/chains/news_collection/internal.md#来源能力与限制)。SC03—SC10 保留职责与模型设计，尚未实现。实现入口存在不等于来源可用、模型有效或投资资格通过。

investment.bak 仅提供理论和业务链路参考，旧版实现、函数划分、字段及运行结果不作为当前实现依据。

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
| SC01 | [基金买卖交易记录](docs/chains/account/README.md) | 买卖了什么、确认多少份额、是否到账 | TransactionContext | 真实交易登记与后续估值、资金及风险分析分开 |
| SC02 | [信息发现与原文采集](docs/chains/news_collection/README.md) | 获得了哪些材料、版本与未覆盖项 | EvidenceCorpus | 获取材料与评价内容具有不同验收标准 |
| SC03 | [事实评审与经济假说](docs/chains/news_assessment/README.md) | 来源支持什么事实、可能经什么机制影响行业 | ResearchAssessment | 事实、机制假说和收益预测分开 |
| SC04 | [投资工具范围与暴露映射](docs/chains/fund_universe/README.md) | 主动搜索完整同类名单、核验身份暴露并保留风险 | InstrumentUniverse | 范围与身份先确定，避免以结果选择分母 |
| SC05 | [时点数据与实验协议](docs/chains/research_dataset/README.md) | 主动取得金融资料、审计时点可得性并冻结实验 | DatasetBundle、ExperimentProtocol | 版本与时间对齐不承担拟合和预测 |
| SC06 | [预测与联合情景](docs/chains/forecast/README.md) | 当前信息条件下的收益、价格及风险不确定性如何表示 | ForecastBundle | 预测方法不自行决定资金金额或授予资格 |
| SC07 | [组合与资金方案选择](docs/chains/portfolio_selection/README.md) | 在真实金融约束下应选什么规则与当前动作 | FrozenPolicy | 目标、资金和费用约束独立于预测方法 |
| SC08 | [独立资格验证](docs/chains/qualification/README.md) | 冻结规则是否有足够预测、经济和金融证据 | QualificationVerdict | 选择与评价隔离，避免循环证明赢家 |
| SC09 | [建议形成与发布许可](docs/chains/recommendation/README.md) | 已验证动作在发布时是否仍适用、用户如何执行 | RecommendationSet | 保持经济决策含义，单独核对时效与执行条件 |
| SC10 | [报告交付](docs/chains/reporting/README.md) | 准确说明完整名单、实际比较及限制，记录交付 | ReportBundle | 报告只解释封存结果，不重新选策略或改金额 |


## 3. 阅读入口

| 目的 | 文档 |
|---|---|
| 开发、验证和审查要求 | [项目约束](AGENTS.md) |
| 总链路、子链边界及模块组织 | [总体架构](docs/architecture.md) |
| 公共交接对象、状态、引用及用途许可 | [公共契约](docs/contracts.md) |
| 必要数据、运行目录、测试隔离及每日说明 | [数据与运行目录](docs/architecture.md#runtime-data) |
| 字段、Schema、规则、SQL及格式示例 | [v1 机器规范索引](docs/spec/v1/README.md) |
| SC01 实现、安装、运行和真实场景验证 | [SC01 内部说明](docs/chains/account/internal.md#运行与验证) |
| SC02 来源插件、运行和真实场景验证 | [SC02 内部说明](docs/chains/news_collection/internal.md#运行与验证) |
| 既往文档审查范围和结论 | [历史审查记录](docs/reviews/20261019-project-constraints.md) |

每条子链从上表业务链路中的子链名称进入：README 负责外部业务说明，internal 负责内部编排与操作；精确规则从子链页面链接到 spec/v1。当前目录、代码包映射及后续示意布局见架构文档。

具体调用记录、请求次数和验证结果保存在该次运行或测试目录；项目文档说明能力与规则，不累积单次执行统计。
