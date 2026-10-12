# SC02：信息发现与原文采集内部设计

文档版本：v1。状态：已有采集框架、来源配置、独立验收及主控；各来源就绪程度见本页能力边界。

[外部说明](README.md) · [公共契约](../../contracts.md) · [总体架构](../../architecture.md) · [SC02 精确规范](../../spec/v1/sc02.md) · [项目约束](../../../AGENTS.md)

子链标识：SC02。说明目录：docs/chains/news_collection/。代码包：[src/investment/chains/news](../../../src/investment/chains/news/)。精确规范：[SC02 v1](../../spec/v1/sc02.md)。

本页规划多来源发现、并发采集和材料组装。网站差异由适配器处理，EvidenceCorpus 的业务含义保持一致。

## 步骤与私有结果

1. **建立采集计划。** 从已登记的信息窗口、来源域、检索协议和账户风险对象生成任务，冻结网站适配器、正文解析器、版本及预算。计划同时建立发现机会清单，区分未选、未取和未处理项。按 source_scope_ref 的站点/来源域选择支持的适配器；不支持来源保留配置和待补证任务，不静默删除。新闻发现只对声明范围负责，不扩展成全球覆盖声明。
2. **发现材料。** 各来源适配器返回标准发现条目，包含来源定位和可核发布信息。检索顺序只决定工作分配，不充当可信度或行业影响评分。
3. **捕获响应。** 按任务取得原始字节，保存请求地址、最终地址、真实获取时刻及结果。每次执行过的请求都形成尝试记录，重试不能删除此前失败。
4. **解析与定位。** 用冻结解析器生成正文及原字节定位关系。发布和可得时间保留来源精度；抓取时间不能反推为较早的可得时刻。网页内容只作材料，不作为执行指令。
5. **归并版本。** 依据稳定文档身份及字节证据关联重复材料和不同版本，保留来源关系及所有获取记录。转载不因网址不同就变成独立佐证。
6. **组装语料。** 汇总结构合格的版本行、检索尝试、解析规范、机会与待处理清单，形成覆盖说明和 Gap，再组装 EvidenceCorpus 与 CandidateOutput。

发现条目、采集任务、捕获结果和解析结果是私有工作对象。其成功、部分成功或失败仅描述处理结果，不对应对外业务许可。

## 网站扩展与并发

统一入口为 `async collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult`。Context 的请求捕获、正文提交和只读冻结配置由框架提供；字段及关系以 [采集接口规范](../../spec/v1/sc02.md#统一采集接口与受信职责) 为准，内部接口不增加外部输入角色。


同采集能力的 Provider 使用统一请求和结果约定，并登记实现版本、支持来源域和私有类型版本。网站特有字段在适配器内转换；业务允许未知的值保留 unknown，结构必填字段缺失仍属错误。

同能力实现可以有不同网站解析方式，统一映射私有捕获和解析结果，由组装器输出同一 EvidenceCorpus；不把网站字段泄漏到公共输入输出。

多个网站可并发执行，分别限制请求量、超时和重试，总预算由子链统一控制。某网站失败不清空其他已取材料；失败范围、预算耗尽和未处理任务都进入覆盖缺口，不能解释为没有新闻或已经完整覆盖。

## 输出与边界

原字节已取得但正文解析失败时，原字节保留为尝试证据并记录 Gap；不能把缺少必填 body_ref 的版本行写入 documents_ref，也不能补造正文。组装器按外部契约输出可核材料，不授予事实可信度或投资资格。

独立验证者检查原文、定位、引用和覆盖声明并签发 ValidationReceipt；调度者封存。新增适配器只需实现同能力接口并登记，运行中更换版本则形成新的执行绑定，不替换原输入。

## 实际实现与恢复

runtime.news_controller.NewsController从run_sc02入口先执行namespace guards，校验本轮身份再冻结input/parameters/clock/window/scope/query和来源manifest/profile。runtime.news_runs在任何process效果前原子持久化running attempt；编号连续1—3，同invocation不能换日重建预算。失败保留failure.json及实际phase/rule/code/pointer/evidence，中断的已开始尝试也消耗预算；已有合法Seal收尾恢复不重新采集。

process_sc02按source_scope选择普通目录内collect.py，各源受信Context共享全局并发/请求预算。每个实际hop均保存Capture，包括redirect、失败、超时；所有来源任务终止后才冻结完整capture-log，来源程序异常不能取消其他来源或伪装正常空结果。按稳定主键产生documents、retrieval_attempts、opportunity_inventory、pending_items、extraction_spec、coverage六表及Corpus/CandidateOutput。无材料且未证明空窗时返回强格式Failure。

主控先绑定实际Candidate和capture-log，再另启validate_sc02，核对独立凭证身份、N01—N12必要检查和用途后写Seal。N10未知覆盖保留insufficient_evidence；部分有效材料可封存为研究观察，goal_status保持unmet。失败凭证及原因反馈到下一次process，输入事实、方法、窗口和规则不变；三次失败返回FailureSummary，禁止后链。

业务证据均在当前runs，SC02不访问账户Store、不新增新闻库。每日manifest/README记录调用、全部文件、状态、失败原因和引用保留条件。注册单测报告、实际业务文件和进程日志分别表达。普通本地HTTP配置复制正式collector算法，用于验证运行路径；真实站点就绪必须由真实响应支持。

## 来源安装与移除

默认插件目录为`src/investment/chains/news/sources/`，目录名即source_id。17个已声明来源为ap、bis、enisa、eu_trade、fao、fed、iea、imo、nbs、ofac、pbc、sec_edgar、usda、usgs、who、wmo、xinhua。每目录含规范manifest.json、extraction.json及异步collect.py，接口为sc02.collect.v1的CollectorRequest/Context/Result。安装新来源须落实入口/材料类别、允许域、实际证据、正文/时间定位、profile SHA256及真实限制；核心按配置发现。完整移除目录后，仍被请求的来源保留unsupported及未知覆盖；损坏安装不能伪装成成功。安装/规则变化用于新invocation，不替换已冻结证据。可用--plugins-dir选择普通目录；test只允许本批根内目录或默认来源，formal禁止test配置。

## 来源能力与限制

17 个来源均保存入口请求依据；pbc、nbs、xinhua（fortune/world）、fed、ofac、eu_trade、imo、who、usgs、fao、usda、sec_edgar、enisa 有实际正文/原生数据校准。Xinhua只采纳已取证的无pageVideo之detailContent文本模板；原视频页仅有制作/编导/编辑署名，保留其原URL、raw、机会和pending_parse，不生成替代正文或转录。WHO 使用页面真实 hubsfiltering OData 配置和同URL外层PublicationDateAndTime；Fed同URL RSS pubDate提供实际GMT时点，正文原日期及release时区文字另保留。USGS 文档页跟随 all_day feed 再取 event detail，保存 GeoJSON；SEC API 文档经 latest/Atom 和 filing index 取得非 XSL 原始 Form4 XML，不把文档页、索引或 periodOfReport 当披露时间/正文。SEC 其他表单、EU presscorner 和 FAO 其他模板尚未校准，保留 raw/pending。

AP、IEA、WMO、BIS 的打包解析配置仍为 calibration_state=unresolved，正文规则尚未完成校准；这些来源不能视为已完整接入。BIS 不能用组织 h1 或 main 兜底正文。PBC/NBS 的脚本分页、Xinhua 动态列表及多数来源历史窗口没有完整性依据；全部打包来源尚无整窗覆盖证明。规则依据和限制保存在对应 manifest/profile，实际访问结果以本轮原始响应为准，不能把过往 HTTP 状态当作未来结论。

新增或修订规则必须先取得真实来源依据并校准独立验证；仅恢复访问不能弥补正文规则缺失。逐次请求、失败与采样细节保存在对应运行/测试目录，长期说明只维护实际支持范围。

## 运行与验证

在插件目录使用已安装的依赖环境；安装命令见 [SC01 运行环境](../account/internal.md#运行与验证)。

公共StageInput的五个角色按顺序为decision_clock_ref、news_window_ref、source_scope_ref、query_protocol_ref、account_risk_objects_ref。最后一项直接引用本轮实际SC01已封存的TransactionContext，run_id相同；SC02只消费身份和许可，不重做交易核算。前四项及parameters按[注册格式](../../spec/v1/sc02.md)构造规范JSON，查询预算、窗口和判断时钟事前明确，不由入口补造。自然语言转该输入的宿主适配尚未实现。

在插件目录执行；input指向选中根的`runs/sc02/<YYYYMMDD>/<invocation_id>/submitted-input.json`或已冻结input.json。占位符替换为真实本轮值；正式根固定为`~/.investment`，正式输入禁止读取test资料：

```bash
PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe -B scripts/run_sc02.py \
  --root "$HOME/.investment" --mode formal --network-origin live \
  --input '<本轮StageInput路径>' --run-id '<本轮run_id>' --invocation-id '<invocation_id>'
```

主控先校验根、模式、插件根和输入命名空间，冻结输入/方法/来源规则及预算，再调用process_sc02.py。候选产生后另进程运行validate_sc02.py；只有必要完整性检查和允许用途成立才封存。N10可诚实表达insufficient_evidence，部分有效材料只支持evidence_review、hypothesis_research、reporting，goal_status仍为unmet。模拟来源永远observation_only。无有效材料且未证明空窗时返回强Failure，不伪造正常候选或成功凭证。

stdout是investment.sc02_run_result：sealed准确引用SealedOutput，failed引用FailureSummary，退出码分别0、1；启动拒绝退出2并只写stderr。相同已完成invocation返回原控制结果，新invocation重新联网。失败原因、已开始的尝试预算跨中断保留，最多三次。业务产物在`runs/sc02/<YYYYMMDD>/<invocation_id>/`，日期manifest/README与调用同级；有效用途结束前保留原始捕获、冻结规则、候选和凭证。本入口不提供自动清理接口。

### 独立验证入口

API：`NewsSchemas()` 加载 SC02 注册，不改 common/SC01 文件；`NewsResolver(root,schemas,*,mode,network_origin,plugins_dir=None)` 在构造时强校验根；`news_dataset(value,schema_id,uri,schemas)` 使用 SC02 表 registry；`validate_output(input_ref,candidate_ref,resolver,*,run_id,invocation_id,attempt_no)->ValidationReceipt` 只读，无网络及候选修正。

```bash
PYTHONPATH=src .venv/Scripts/python.exe -B scripts/validate_sc02.py \
  --root '<selected-root>' --mode test --network-origin simulated \
  --plugins-dir '<this-test-root>/plugins' \
  --input '<invocation>/input.json' --candidate '<invocation>/attempt-01/candidate.json' \
  --run-id '<run_id>' --invocation-id '<invocation_id>' --attempt-no 1
```

验证器参数语义、凭证与退出状态见 [验证入口结果](../../spec/v1/sc02.md#验证入口结果)。

### 实际场景单测

场景入口每次创建`~/.investment/test/<id>/`，先真实执行同t零持仓SC01，再运行同主控、实际HTTP采集和独立validator。本地HTTP使用正式collector代码及普通配置；事前保存注册admission及独立预期，事后生成investment.sc02_unit_report的verification-report.json。业务Corpus不含单测统计；正确拒绝的case可passed=true，而业务execution/output/goal仍false。

```bash
PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe -B scripts/verify_sc02.py --case multi_source
# 其余selector：wait_all_partial / pagination_duplicates / unknown_time_or_layout
# plugin_add_remove / all_failed_three_attempts / fresh_invocation / isolation
PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe -B scripts/verify_sc02.py --live-sources all
```

live-all 使用固定的联网验证场景输入：SC01/SC02共用构造前真实UTC t，窗口[t−7天,t)，全部17来源的全部声明入口/材料类别；并发8/每源2、超时10000ms、响应8388608字节/解码16777216字节、全局请求256、每源页8/文档4、redirect3，UA为Investment-SC02/0.0.1 live-validation。预算不定义全集，全部发现机会及未取/失败项保留；七天观察区间不是正式研究默认周期。

### 验证器校准

`tests.test_sc02_validation.ValidatorCalibrationTests` 的四个现有方法分别校准本地实际 HTTP 支持材料、真实 body/time 复制错误、遗漏配置源/发现机会、有限列表加真实来源失败时的诚实部分覆盖。它们实际运行隔离零持仓 SC01 取得合法上游，用独立预定正文与时间构造候选，再调用同一 CLI；此阶段不声称 SC02 producer 已运行。

支持材料/正文时间两个 selector还校准实际格式回归：完整原生GeoJSON空根、XML及HTML；Xinhua文本模板与pageVideo署名区域的区别；Fed RSS/WHO外层准确URL发布时间、指定窗外排除及冲突时点unknown。它们仍是同四个selector中的回归变体，不新增来源能力或测试入口。
