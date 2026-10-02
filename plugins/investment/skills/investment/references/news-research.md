# 免费原文、经济观测与源版本

schema4，插件0.0.1。analysis_start冻结run_id/news_policy/publish_cutoff；news_collect和news_assess携同run，取得真实原文后再分析。

## 采集与覆盖

来源授权以已提交analysis_run为准；该run封存news_policy_hash/source_registry_hash与真实registry_snapshot_ref。默认采集仅遍历policy.sources；显式集合必须是它的子集，required_source_groups也不得越界。collection保存选择模式、selected_source_ids与同一snapshot/hash；assessment和validator读取原始run，不能从已采manifest扩大来源或更新policy。

source scheduler、watermark和scope key同时绑定registry/policy hash。原文document/version、first-observed/first-known仍是事实身份，不因policy改变而重写。PDF/父公告继续使用同一来源注册规则及真实原链接，不扩大来源或域名；预算不足保留partial。publish_cutoff仅为原检索发布边界，控制器在真实采集完成时封存information_cutoff_at及producer journal；capture≤information_cutoff≤review≤decision才支持当次可得性。精确时刻和date-only统一按实际采集时点核验，出版关系另列before/after/overlaps_date_interval，日期不补造午夜。原信息封存前回放不能使用后来取得的事实，没有快照的旧记录不得补造新授权scope。

注册source_registry限定官网/路径/正文/日期/HTML或PDF容器。发改委有新闻、政策公开及解读；Gov/NBS/PBC/CSRC/Fed/ECB使用实际适配。PDF必须保留父公告、原链接和原时间；不通过手填标题/发布时间让独立附件过关。HTTPS证书校验保留，正常传输失败如实记录，可真实重取，不修改产物。

国家医保局政策源从原CDATA记录发现文章，按原PubDate与zoom正文核验；不代表药监审批、整站或企业公告全覆盖。来源按主题由required_source_groups声明，缺专门来源不能用宏观来源数量冒充该主题已覆盖。

collect.payload包含cutoff_at/window_start/required_source_groups/run_id，可声明sources（明确文章集合）、timeout_seconds、max_urls_per_source及budget_seconds。发现与正文分开，有限分页/最近已跟踪原文重抓修订，记录未取得、预算、URL及cursor。完成仅表示该明确集合，非全球/整站/整日全覆盖。没有发布时间的正文不冒充当天消息。

默认模式持久轮转来源及列表/正文FIFO，自动为实际请求正文形成明确集合；scheduled_article_urls在fetch前记录，不能移除失败正文制造完整。discovery_pending_urls单列未处理链接，断点继续而不是每次先重复默认列表。PDF父链接任务跨操作保存，在当前操作重抓原父公告再取得附件；不复制旧时间冒充新抓取。

父公告与PDF的原子依赖至少需要每源两个URL请求额度。普通正文额度1可用；已知PDF依赖且额度1时明确要求minimum_max_urls_per_source=2并保留待采，不静默重复父公告、提高预算或认证PDF完成。

捕获保存原文字节、跳转、media/encoding、hash、retrieved_at、真实publish/modified精度；完整中文“年/月/日”按原日核验，日期无时分不补造。不得把不完整或彼此分离的数字拼成日期。body/lead/partial/unavailable分别处理，只有合格body可建立源事实；未取得正文不等于没有新闻，拒绝数与覆盖率不作成功KPI。

## 源事实与经济内容

assess.payload含collection_id、claims、events、industry_theses，可附economic_observations。claims逐字引文与raw version核验；事实/推断、反证/缺项、审阅身份保留。行业方向是研究假设与检索，不是模型利好分数。

thesis量化绑定sector_id及正式行业定义；economic observation含稳定event/sector/category、指标ID/原文名称、单位/口径/期间、客观动作和actual/prior/expectation的原数字、quote、version与locators。actual不能来自预告，统计实绩期间不能晚于原发布时间；政策计划额度作为政策事实单独归类。source forecast只有发布前可靠证据时用，不自动称市场共识。单位/指标不同不合并绝对变化；缺失与零分母有明示状态。

calendar期间必须取引文中完整日期的最大跨度，规范期间及day/month/quarter/year粒度均与原文一致；不能把“2020年3月15日政策利率3%”中的期间提交为“2020年3月”并制造3月31日或月频。原文明示的月、季度、年观测仍按对应粒度保留；不支持的歧义、区间或不完整期间保持未知或拒绝，不靠截短、拼接或聚合取得资格。`period_label`保留原字面标签，calendar类型须由完整源期间核验；生产及独立原文重建遵守同一合同。

普通单值不设`value_role`。仅在同一完整原句明确给出“原期间＋原指标，由/从原值及原单位，调整/上调/下调为/至新值及同一原单位”的唯一关系时，允许`value_role=before|after`；原期间、指标、单位和两值均须直接锚定原句，方向与数值矛盾则拒绝。多个指标、单位不明、角色不明或关系不唯一保持缺口，不拆拼原句或强选某个值。

仅新typed transition的measurement增加`value_role`与`source_transition`。current为after、prior为before，且version、原quote、指标、单位、期间和source_transition全部一致时，观测增加`comparison_basis=source_stated_transition`，允许同源同日转换；新增角色、转换关系及basis参与身份与独立原文重建。普通prior仍须更早的真实calendar period。已有有效单值及跨期原文事实对象、键和first-known保持原样，不补None、角色或basis字段；这是保留真实来源事实，不是错误模型回退。同日转换不是跨期历史，不能把两端值当作两期历史样本或事前预期。

source_revision_id仅由真实源版本、事件/行政区间及源撤回关系生成；分析revision可含review_by。observation_key排除分析续期/局部claim ID和引文span，真实首次known_at不可因重审刷新；每份完整记录仍有可追溯observation_id。跨日同一sector/event与版本连接历史，不依赖重新命名thesis。数值、预期、动作和指标同义证据在生产与验证时都使用权威run的registry_snapshot_ref；首次观察还核验原采集日志、原注册表和原文字节，不能通过可变注册表或伪造早期时间取得资格。

event保留event_key、version_ids、claim_ids、event_at/effective_from/effective_until/review_by、supersedes/retracts和原文temporal_evidence。正式文号或原URL支持修订关系，不从内容相似猜撤回。行政时效与研究复核不同，验证按当时已知知识重建，不用后来撤回改写旧判断。

## 数值与资格

正式输入模式为 `source-news-asset-en-v2`，流水线为 `source-news-asset-en-fund-en-v2`；旧模型、旧编码和旧三项行业桥不能作为回退。80项类别/动作字段加4项事件状态、8项来源辅助字段，共92项基础字段；成熟train内再学习指标词表及数值、缺失和动作交互。指标概念、规范单位、测量类型、原文期间频率分开；名称改写必须有原文同义关系证明。prior/expectation要求相同单位、口径及期间频率。未知未来概念不能静默编码成零。

每个active事件×实体单列 `measured`、`qualitative_encoded`、`unsupported` 或 `source_gap`。政策动作使用真实条款的类别与动作指示量，收益方向和强度由过去标签拟合，不设置“利好分”。一个旧GDP测量不能替另一个新政策事件消除缺口；无实体映射、缺原文或经济编码缺失保留待核。相对变化无量纲，source forecast差不冒充历史σ或市场共识，92项设计不宣称科学最优。

industry_prepare.payload绑定analysis_run_id/news_review_id/spec，可给动态benchmark_contracts；sector_id作为规范实体ID，不限定为某一大陆单行业标签。资产输入域注册equity、bond、commodity、foreign、mixed、fof。单行业、多行业、主题、市场、利率、久期、信用、商品、汇率、对冲、穿透和风格是不同角色；分类版本、原文定位、关系引文和真实known_time必须保留，不使用付费GICS资料或猜测标签对应。

公开CSV输入核验原始series_id、字段、身份原文、单位、币种、时区和观测日历。利率/利差的percent、basis_point或decimal_rate没有货币单位，观测currency=NONE；油价按原文USD/桶，均为经济协变量，基金交易仍为天天基金CNY。币种与原单位相符，FX方向及注册换算单独核验；大陆指数的CNY身份限制保留。百分数/基点使用规范水平变化，允许0和负值；价格目标明确使用价格对数变化，不能把十年收益率说成债券总回报。source日期按来源时区解释，日期精度不冒充基金实际下单、估值或成交时钟。新取得的旧数据保持真实system_observed时间；没有可核历史版本证明，不补造market_public_available或过去已知。

基金桥为12角色各两项预测和一项可用指示量，再加当前未知资本、陈旧披露参考和模型估计指示量，共39项协变量。相同源披露类别的参考权重去重，主题重叠不产生第二份资本。历史披露（包括同日、仅日期精度）不证明当前盘中持仓；`actual_current_unknown_weight`保留为1，`disclosed_reference_unmapped_weight`仅说明已披露参考范围。混合/FOF有真实公开穿透时保留原始时点及陈旧状态；缺真实持仓可使用仅过去原NAV/资产资料拟合和校准的EN风格系数，标明model_estimate，系数可有符号，不正规化为真实仓位，不作为行业硬上限的当前持仓证明。

所有资产预测、汇率及来源风格仅为基金EN协变量。人民币基金NAV已包含底层价格、汇率及基金内费用，不能再向基金现金财富叠加资产、FX或管理费收益/损失；投资者交易费用和权益按同一人民币现金账处理。完整来源版本与监督标签成熟边界保留，当前模型冻结在校准以前。数学见[模型](allocation-method.md)；原文引用一致与描述性样本外差异不等于政策因果或可盈利证明。
