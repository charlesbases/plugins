# SC07 内部流程：规划资金与政策选择

文档版本v1。状态：仅契约/依据阶段，待根审核；以下是后续实现约束，不是已执行证据。

[外部说明](README.md) · [精确规则](../../spec/v1/sc07.md) · [共享运行契约](../../spec/v1/research.md) · [项目约束](../../../AGENTS.md)

## 实际调用和职责

run_sc07将交由共享controller冻结本轮输入、方法/参数、schema资源与真实attempt，再独立调用process_sc07与validate_sc07。process/chain.produce只生成候选或实际HostRequest；validate/validation.validate读完成候选、原合法输入和自己的宿主工作，独立返回凭证。共享controller按完整规则与用途映射seal或带原因重处理；首次加两次失败后停止。producer不自签、validator不改候选，重试不换本轮事实、方法、cutoff或预算。

1. 绑定：按method.runtime.input_roles核准确role、上游真实seal及最低读取用途；ctx.upstream_receipt保留权威blockers，数值用途另核高级许可。SC01读取冻结generation/ledger_digest和交易/持仓/pending，不读latest；FundingPlan本轮独立输入。UserMandate通过已验SC05协议参数逐ID消费，缺偏好保留unknown，不从净交易额推断。
2. 范围：原U、逐成员peer组、实际风险与审计不变。依据冻结comparison_rule建R，未知成员仍R；只承认原证据支持的排除或目标非重大性证明。任何取材、模型或预算失败均保留对象与Gap。
3. 合法空间：读取授权current/selection物理视图、合同/日历、原NAV/分红和SC06实际共同网格；逐真实时点、份额批次和状态构建合法金额/份额网格。先量化再评价，未证条款不默认零或放行；网格at仅可能是期间upper索引，不能推出精确成交/到账；无中间价格不插值，数据充分性统一按动作需求判定。
4. 规划账本：本轮可部署上限作模型现金额度，登记持仓、已付款未确认和未到账应收分开一次计价。历史已到账销售不重新增加预算，卖出确认移份额→净应收、到账移应收→现金；不得重复扣份额/费用。各方案完全共用起点/资金计划/币种/期限/财富口径。
5. 政策计算：冻结完整PolicySpace、模型更新与非前视规则；实际逐政策、逐共同情景推进精确动作和金融状态，保存模拟动作/状态、财富、现金及配对基准。计算费用后E[W_H]、B-W_H及终点CVaR，逐状态核现金/锁定和独立用户约束。仅当前首步进入current_actions。
6. 求解与清单：真实完成路径形成A与execution证据；R-A和未计算政策均保留。有限合法集合按独立穷举可验，其他算法须可复核上下界；预算耗尽只best_found，不改原范围/政策空间。冻结规则、当前精确动作及所有缺口后提交独立验收。

## 正式宿主与独立验证

后续正式模板登记为prompts/sc07/policy_science.md（producer）和policy_review.md（validator）；当前契约阶段不创建模板。两角色通过共享HostRequest请求Codex宿主真实新上下文工作，保存原文、event与response。工作key来自冻结对象/政策计划，不按测试名称切换提示词。无真实调用不得造响应；坏/failed响应按共享失败与attempt预算处理。

policy_science核科学前提/政策定义与局限，不由模型随意报金额；policy_review独立检查来源/模型适用性与候选，不能读producer私有宿主原文。验证器自己按SC01整笔事实、FundingPlan和合同公式重建状态，CVaR用最差概率质量法核，与producer min公式独立；合法小集合另行枚举。可共享Schema、引用解析和精确十进制基础，不共享producer业务模拟/选择算法作oracle。

宿主预算按ctx.host_work_summary本attempt两角色已登记(role,step_id,work_key)计，含pending/failed；成功工作仅completed并真实绑定。重试另attempt而最多三次，不能改key或删失败释放名额。共享控制事实接口不增加JSON包装或raw权限。

## 可达校准与未执行项

| selector | 实际前置和独立核 |
|---|---|
| settlement_cash | SC01先实际买入10份再合法卖出；NAV10/费1%应收99；到账前不能用99买入，到账后不再扣份额 |
| fee_accounting | 合同支持申购外扣/赎回和分红/NAV口径；独立核gross/(1+r)、净应收和费用只计一次 |
| finite_policy_comparison | 小合法有限政策空间，共同情景/合同完整；独立穷举赢家、精确并列；未知成员U/R保留，预算耗尽仅best_found |
| cvar_scope | 上游合法等概率损失10/30、β=.5得CVaR30；另核边界原子分割及终点/路径风险区别 |

完整准入记录在method.calibration：实际入口、合法上游/合同前置、触发、synthetic来源、独立预期、P规则映射。资料/模型不足明确未执行，不造已验seal，不构造零持仓卖出或测试专用prompt。静态Schema加载、基础数值算例、validator校准和完整真实流程分别报告，不能互相代替。

后续实现顺序仍为根审契约→validator及独立校准→根冻结→producer/正式模板→真实流程单测→fresh review。当前没有执行任何后续阶段；SC06/SC05合法封存和必要时点资料仍是运行依赖。

## 目录与限用

正式root精确为~/.investment；全部测试/联网/缓存/辅助严格置~/.investment/test/<batch>/，命名空间校验先于读/锁/建目录。候选/表/宿主材料在本attempt，跨轮必要完整政策仅按准确引用保留。全部命令显式Bash；业务Python使用PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe -B。SC07不扩共享Schema、不改上游、不借审计/参数祖先读取heldout/raw；证据不足只撤相应用途、保留对象与实际失败。
