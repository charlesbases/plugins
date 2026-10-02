# 状态、事件与持久化

## 归属与首次判断

默认root为 `$HOME/.investment/`，允许用户明确指定其他root；所有请求始终使用同一个root和明确plan。数据与插件安装目录分离。当前统一入口可建立控制数据库，但没有account/main记录时仍是首次理财，研究或试验数据不能自动变成真实账户。

```text
<root>/plans/<plan>/
  active.sqlite
  history/YYYY/MM/sealed-<sha256>.sqlite
  objects/YYYY/MM/<hash-prefix>/<sha256>
  work/<operation-hash>/<generation>/<attempt>/<stage>/
<root>/runtime/
```

当前公共请求、账本与数据库为schema4；旧数据库必须先执行独立离线升级，拒绝旧版时不修改源库。active.sqlite维护当前状态、不可变事实、操作与阶段记录；只读历史分卷仍参与全局ID查询。对象按内容寻址及时间分区，大于16 MiB的对象分块。每个数据库JSON值的存储编码限制1 MiB；更大的逻辑记录、操作请求及结果自动完整封存到不可变对象，数据库只存有界引用，不能截断候选或原始事实。数据库达到64 MiB触发归档，达到128 MiB的有效活动数据上限会明确停止增长，不能删活状态掩盖容量问题。分卷先校验内容及数据库完整性，再事务提交目录与转移记录。

大值使用JSON语法外的保留前缀`@investment-cas-json:1:`和引用封套；普通用户对象或字符串不会与封套混淆。引用清单自身过大时也封存为对象。get、scan、operation、audit及归档读取统一还原完整逻辑值并验证内容，记录和请求的哈希仍针对原逻辑JSON；这是schema4的存储编码，不是旧业务模型兼容运行。归档保留原编码，备份必须同时包含active.sqlite、history和objects。业务引用发布仍经过租约fence；失败或失主执行者至多留下未被引用的不可变对象。对象保留量随历史增长，单文件/数据库容量约束不代表总存储自动封顶。

## 反馈契约

feedback.payload包含currency、events，可带account_id和按事件ID关联的evidence；真实反馈账户为main，当前仅支持CNY。每个事件为：

```text
id、type、effective_at、known_at、recorded_at、data
```

sequence由系统分配。effective_at是经济生效时间，known_at是已知时间，recorded_at是记录时间；三者带时区且顺序合法。金额、费率、份额等财务值用精确十进制字符串，布尔值不能替代数字。未知资料用unknown记录；resolve_unknown必须给出实际可读取、摘要匹配的artifact对象引用，字符串标签不能解除阻断。

sell_fill.data可携带带时区的actual_redemption_confirmed_at，表示回执明确记载的实际赎回确认时点；其当地日期不得早于price_date，时点不得晚于known_at或recorded_at。它属于原始成交内容，晚收到回执不改变该时点，也不后台补写已存事件。合同以确认日结束持有费期时仅用该字段；以执行日结束时用实际price_date。确认终点未知时保留原报fee、net_amount和应收款，追加expected_quote_unknown及补核任务，不将known_at猜作确认日。模拟事件的确认时点按来源确认规则生成，逐订单保存。

账户内的fill_id、payment_id、order_id、distribution_id及应收结算身份由financial_fact永久索引，归档后仍去重。这些已索引事实改event.id时保存别名；同身份不同内容报冲突。外部资金流不再接受feedback直接提交，必须经过cashflow_prepare/confirm/correct。provider身份绑定账户、机构及原始流水号；人工转账先生成稳定草稿，并明确选择关联已有或确认新笔。同金额同日期仅提示疑似重复，不能据此自动去重。transfer_id下的revision_id/previous_revision组成追加修订链，撤销以金额0的新修订表达；真实退款仍是另一笔转账。平台日内编号须结合交易日等来源字段构成全局身份，依赖时间的身份统一为UTC。

| type | data的用途 |
|---|---|
| opening、valuation | 初始现金/批次/价格；每个资产必须提供price_dates |
| cashflow | 由资金流确认接口生成，含transfer_id、revision_id、previous_revision、amount、valuation |
| order_reserved | 确认提交的订单及现金/份额占用 |
| cancel_requested、cancel_confirmed | 取消申请与确认分开，确认前不释放 |
| buy_fill、sell_fill | 唯一fill_id的已确认成交；明确price_date、gross_amount及实际cash_debit或net_amount；买入另含holding_started_at和ownership_at，赎回可含actual_redemption_confirmed_at |
| subscription_pending、subscription_confirmed | 实际扣款但份额待确认的资产，及后续确认 |
| settlement、dividend_paid | 以实际amount确认既有应收到账，偏离预计日期/金额进入对账 |
| dividend_declared、dividend_reinvested、split | 分红必须明确record_at、entitled_shares；现金支付与确认再投分别记录，不能把除息日持仓当登记权益 |
| external_fill_confirmed | 没有预测订单的真实外部成交，金额、份额、价格日、持有起点、权属日期及原始receipt均须明确 |
| account_snapshot_confirmed、lot_metadata_confirmed | 用完整原始对账单补充当前敞口或确认批次日期；不重置本金，不自动解除unknown，历史收益缺口仍保留 |
| unknown、resolve_unknown | 待核事实及有证据的解除 |

各类型data严格按ledger.EVENT_DATA及相关校验构造，不支持的公司行动不得静默估算。提交、部分成交、最终成交、取消和到账不是同一个状态。机器检查结构及算术，不据此证明用户确认或来源文件真实。

## 账户归约

首次确认前允许有unknown等观察记录。只有完整历史中不存在任何已知经济事件、现金/份额/单位数/在途/资金流均为空，并且用户另外明确确认`confirmed_no_prior_economic_activity: true`时，`profile_initialize`才可建立首个绩效起点。它内部追加`performance_start`并保留先前观察历史；公共feedback不能提交该内部事件。有既往交易、资金流或损益的账户不能用此路径重置。预算不等于已经到账现金，迁移不会代填确认。

```json
{
  "schema_version": 4,
  "request_id": "replace-with-stable-first-confirmation-id",
  "operation": "profile_initialize",
  "payload": {
    "principal": "REPLACE_WITH_USER_CONFIRMED_ACTUAL_CASH",
    "currency": "CNY",
    "loss_tolerance": "REPLACE_WITH_USER_CONFIRMED_FRACTION",
    "as_of": "REPLACE_WITH_CONFIRMED_ACCOUNT_TIME",
    "confirmed_initial_all_cash": true,
    "confirmed_no_prior_economic_activity": true,
    "account_hash": "REPLACE_WITH_CURRENT_ACCOUNT_HASH",
    "user_source": {
      "message": "REPLACE_WITH_EXPLICIT_FIRST_ACTIVITY_AND_AVAILABLE_CASH_CONFIRMATION",
      "confirmed_at": "REPLACE_WITH_ACTUAL_CONFIRMATION_TIME"
    }
  }
}
```

以上是不可直接执行的占位示例；缺任何实际确认时继续询问，不将示例布尔值或金额写入真实计划。

资金流估值的原始用户说明会封存为字节工件。仅有可读原文引用不能获得独立数值核验：`valuation.source_market_id`必须绑定系统生成的市场记录，其逐基金、逐日期的已审计`nav_ref`须与估值金额匹配，且`valuation.at`等于真实资金流经济时点。缺该绑定或数值矛盾时，现金仍入账，收益保持`performance_pending`；后续以`cashflow_correct`追加核验后的估值。该证据范围是对应经济日期的基金净值，不是未来收益或盘中可交易价格保证。

持有基金已知除息而最新价格仍是除息前净值时，不将该价格与分红应收直接相加发表风险预算，也不以“价格减分红”构造新净值；取得原始除息后净值才能继续。未曾持有的研究候选不产生账户分红权益。所有未确认买卖订单都需要确认或取消后再发布新的量化建议。

capital_baseline保留原始本金，capital_revision追加有来源的更正，capital_current保存当前确认基线。cashflow修订影响原基线时须capital_reconcile，不据市值自动重置本金或抹掉历史亏损。净投入不再为正时保留全部事实并要求确认剩余资产风险政策，不制造正本金。risk_revision保存基本、临时及提前结束修订，risk_profile保存当前修订指针。每次计算从基线及之后已确认外部现金流推导净投入本金；临时风险到期恢复；end_temporary可明确提前结束，temporary+target_revision可替换旧窗；base必须明确end_active_temporary，并返回effective_loss_tolerance，避免新基本值实际仍被临时值覆盖却不说明。plan_constraints单独版本化平台、币种、目标、排除类别和基金组/行业限额；新确认不接受人工复核或持有天数。旧修订的完整内容、哈希及用户来源保留，退役元数据只作历史证据，业务约束投影沿用当前唯一校验与计算规则；费用年龄从源合同产生，不改写真实本金或成交。示例数字不写入真实账户。决定绑定账户和风险两个版本，提交事务内重新检查有效期。

ledger是唯一账户投影计算。已确认部分成交入账，剩余资金/份额继续冻结；取消申请不释放。扣款待确认不算可用现金；赎回应收、分红应收仅在确认到账事件后可支出。重复事件通过跨活动库/历史分卷的ID与内容摘要检查，变更内容拒绝。

晚到但真实的确认触发按经济时点重建。历史观察分别限定已知截点和经济截点，不能让后来获得的事实回流为当时决策已知。未知风险暴露、待确认份额或关键价格缺失会阻断相应快照，不把未知估成零。真实现金流先更新现金；缺少可核验的资金流发生时点估值时标performance_pending，unit_nav/net_return为空，已确认可用现金不会归零。当前可靠价格下的权益可独立计算，正式试验必须performance_exact。未完成买单单独列pending_buy_orders并阻断新量化建议，仍可监控、记录事实和处理取消。

## 原子性与恢复

反馈批次、事件所有权和账户投影在一个事务提交。最终决定和最新指针在校验通过、账户版本CAS成功后与操作结果一起提交；规范报告只引用已经验证的对象。

request_id及payload相同重试复用已完成结果；不同payload必须新ID。操作租约使用owner token+递增generation，定时心跳续租。所有普通写事务进入及提交前检查当前所有权、有效期，stage/fail/complete也受同一保护；失主不能发布旧结果。离线maintenance是显式排他能力，公共业务不调用它。research/TSA可变工作目录按generation/attempt隔离。允许失主留下无引用的不可变CAS字节；业务引用和最终发布仍必须通过fence。外部抓取及TSA只承诺可重复发送，不声称远端恰好一次。归档仍保留全局幂等身份；没有“换个月就可重复成交”的路径。

audit检查数据库、内容指纹及指定决定/报告；archive执行受控分卷。不能用宿主文件工具直接改账本、投影、新闻游标或发布状态。用户授权的数据删除由相应维护任务处理，默认研究不删除历史。

摘要和签名都不能防止拥有整个目录控制权者整体替换所有记录，也不证明不存在未披露试验；能力范围必须如实说明。

所有历史分卷和对象的总容量仍需观察；单条/单卷上限不等于自动保留期限。原始确认与对账差异不可为通过计算而删除；语法有效但不能投影的成交保存为未对账事实，补充原始确认或完整账户快照后，再以明确resolve_unknown恢复当前分析。
