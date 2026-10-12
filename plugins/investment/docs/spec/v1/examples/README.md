# SC01 强格式输入示例

全部为synthetic_fixture构造材料，仅说明字段格式，不是实际用户账本、执行结果或验收缓存。不保存expected输出。引用、执行身份、时间和base_generation须在真实调用时按实际SQLite与文件字节重新构造。

| 文件 | 类型与前提 |
|---|---|
| initial-source.json | SourceRecord；首次确认零持仓，变化/回复为空，generation=0 |
| confirmed-buy.json | Trade；在已建立零起点后买入确认1000份，实际含费支付2000，费用5 |
| pending-buy.json | 另一独立Trade；实际投入100，份额及实际时间未提供，预计整笔确认 |
| confirmed-sale.json | 已持有上述1000份后的一笔卖出；确认200份、整笔净应收385，尚未到账；登记份额800 |
| received-sale.json | 同一sell-001整笔到账，份额仍800，不追加第二次扣减 |
| reply-postpone.json | 同一sell-001未到账答复及未来新预计；不能与received-sale的实际到账同时应用 |
| bad-decimal.json | 声明反例；investment.trade按J02拒绝JSON number金额 |

示例分支须遵守前提，不顺序应用互相冲突的事实。正式流程验收运行scripts/verify_sc01.py --self-test；它创建新的空根，首次零持仓，之后重读上一轮实际数据库，经正式CLI及独立校验进程产生结果，不读取这些示例作为预期。
