# plugins

`rning/plugins` 是 Codex 插件市场，市场名称为 `rning`。当前提供 Cortex 和 Cortex SE 两个工程开发流程插件。

## 目录结构

```text
.
├── .agents/plugins/marketplace.json
├── README.md
└── plugins/
    ├── cortex/
    │   ├── .codex-plugin/plugin.json
    │   ├── hooks/
    │   ├── scripts/
    │   └── skills/
    └── cortex-se/
        ├── .codex-plugin/plugin.json
        ├── hooks/
        ├── scripts/
        └── skills/
```

插件说明和通用验证流程统一维护在本 README。市场配置中的来源路径相对于仓库根目录解析。

## 安装

先安装 Codex CLI，并确认当前版本支持 `codex plugin`。以下命令使用 Bash；Windows 可使用 Git Bash。

### 从 GitHub 安装

注册市场：

```bash
codex plugin marketplace add rning/plugins
```

按需要安装插件：

```bash
# 安装 Cortex
codex plugin add cortex@rning

# 安装 Cortex SE
codex plugin add cortex-se@rning
```

查看市场及插件状态：

```bash
codex plugin marketplace list
codex plugin list --marketplace rning --available --json
```

`@rning` 对应 [marketplace.json](.agents/plugins/marketplace.json) 顶层的 `name`。安装完成后，新建 Codex 会话使用插件；桌面端未刷新时重启应用。Hook 需要按宿主要求完成信任设置，安装成功不等于 Hook 已实际运行。

### 从本地源码安装

从仓库根目录注册本地来源：

```bash
codex plugin marketplace add "$PWD"
codex plugin add cortex@rning
# 需要 Cortex SE 时执行：
codex plugin add cortex-se@rning
```

GitHub 来源和本地源码来源使用同一个市场名称。切换来源时通过 `codex plugin marketplace list` 核对当前配置及注册结果，确认安装来源指向目标仓库或源码目录。

## 更新

### 更新从 GitHub 安装的插件

先刷新市场的 Git 快照，再安装所需的新版本：

```bash
codex plugin marketplace upgrade rning
codex plugin add cortex@rning
# 已使用 Cortex SE 时，也更新它：
codex plugin add cortex-se@rning
```

`marketplace upgrade` 刷新市场来源；随后通过 `plugin add` 安装所选插件。完成后用 `codex plugin list --marketplace rning --json` 核对版本及状态，并新建会话验证更新结果。

需要清除某个插件的旧安装缓存并重新安装时，以 Cortex 为例：

```bash
codex plugin remove cortex@rning
codex plugin add cortex@rning
```

Cortex SE 使用 `cortex-se@rning`。重新安装会改变本机安装状态，不是只读检查。

### 修改本地源码后更新

1. 明确改动范围，执行下文的通用插件验证流程。
2. 更新实际修改插件的清单版本；Codex 兼容格式的本地迭代可使用 `plugin-creator` 的缓存标记辅助脚本，具体示例见“本地安装与版本核对”。
3. 确认市场配置指向待验证的源码，再从本地市场重新安装并核对安装资源。`marketplace upgrade` 针对 Git 市场快照，不能替代本地源码的版本更新。
4. 在新会话中验证安装版的发现、触发及实际行为。
5. 发布到 GitHub 时提交源码、版本及必要的市场配置，并推送仓库。其他设备再执行上述 GitHub 更新步骤。

发布前确认 `.agents/plugins/marketplace.json` 已纳入 Git；如果它被全局忽略规则过滤，可在仓库根目录执行 `git add -f .agents/plugins/marketplace.json` 后再提交。不要为了更新某个插件而修改全局 Git 忽略规则。

## 插件说明

### Cortex

#### 概述

Cortex 是用于工程设计、源码追踪、计划、实施和验证的技能集合。具体约束以各技能文件为准。

```text
需求 → using-cortex 路由
     → 必要的 code-tracing / systematic-debugging
     → brainstorming 需求分析、澄清及设计
     → writing-plans 完整方案及一次确认
     → executing-plans 或 subagent-driven-development
     → 适用的 testing / requesting-code-review
     → verification-before-completion
```

只读分析、代码审查和不写文件的代码答复按各自入口执行，不强制走实施确认流程。`testing` 只在用户显式调用时进入。

| 技能 | 职责 |
| --- | --- |
| [using-cortex](plugins/cortex/skills/using-cortex/SKILL.md) | 路由、范围与确认状态 |
| [brainstorming](plugins/cortex/skills/brainstorming/SKILL.md) | 需求澄清、设计、修订前重新取证及语义核对 |
| [code-tracing](plugins/cortex/skills/code-tracing/SKILL.md) | 与当前结论相关的完整源码链路 |
| [writing-plans](plugins/cortex/skills/writing-plans/SKILL.md) | 完整方案、调用链、逐文件伪代码、验证与唯一方案确认 |
| [executing-plans](plugins/cortex/skills/executing-plans/SKILL.md) | 直接执行已确认计划 |
| [subagent-driven-development](plugins/cortex/skills/subagent-driven-development/SKILL.md) | SDD 任务简报、实施与审查交接 |
| [code-standards](plugins/cortex/skills/code-standards/SKILL.md) | 生成代码与新增单测的质量约束 |
| [systematic-debugging](plugins/cortex/skills/systematic-debugging/SKILL.md) | 先证明根因，再交接修复 |
| [requesting-code-review](plugins/cortex/skills/requesting-code-review/SKILL.md) | 基于证据的只读审查 |
| [testing](plugins/cortex/skills/testing/SKILL.md) | 显式测试点、纳入完整方案的测试映射及执行 |
| [verification-before-completion](plugins/cortex/skills/verification-before-completion/SKILL.md) | 根据最终证据报告结果与未完成项 |

#### 工作流程

每个实施方案固定确认一次，不由模型根据需求清晰度决定是否跳过。方案包含需求与约束、源码上下文、设计决定、端到端调用链、逐文件新增/修改/删除及伪代码、影响、验证和执行方式；调用链与伪代码以代码为主，文字补充原因与约束。三档调用链形式和方法级单测选择以 writing-plans 为准。

执行方式在方案中提前写明，默认直接执行，用户指定 SDD 时将任务及恢复记录纳入方案。完整方案后的明确 `y` 或实施指令批准当前方案；回答澄清问题或选择设计方向只更新输入。方案批准后直接实施和验证，不另行确认设计、计划、执行方式或已纳入方案的测试方案。

```text
异议 / 新方向 / 设计收窄 / 口径变化
  → 暂停受影响的实施
  → 回看需求、既有约束和新意见
  → 重新读取相关实现上下文
  → 核对入口、校验、调用、分支、值或状态、生命周期与最终边界
  → 核对新方向的可行性、语义及影响
  → 展示修订后的完整方案
  → 确认当前版本一次 → 实施
```

候选字段、表达式或简化路径必须与需求结果、值定义、计算和发生阶段一致；冲突要作为新的设计决定呈现。用户明确改变目标时记录新目标并重新核对。旧证据仅能复用仍适用的事实，文件未变化不等于新方向已被证明。

技能切换和任务恢复保留可恢复的有效批准。局部实施细节、进度和测试结果更新不重置批准；文件或目标范围、关键调用/分支/状态、输出口径、接口或外部边界、验证范围、执行方式变化则更新完整方案后确认。验证失败先证明根因；符合批准关键决定与范围的修正直接实施并重验，改变方案的修正重新取证和出方案。

`testing` 保留显式调用入口。测试点、操作、预期、命令映射、报告和可读批准基线任务应在完整方案确认前呈现；已纳入批准内容的测试直接执行，独立发起 testing 则呈现完整测试方案并确认一次。报告语义与命令映射变化必须更新方案，结果及勾选更新不改变批准基线。

### Cortex SE

#### 概述

Cortex SE 是工程开发流程技能插件；具体职责和约束见 [skills](plugins/cortex-se/skills/) 目录。

Cortex SE 同样要求基于源码证据、明确实施路径并验证结果。明确的实施请求可以在建立相关上下文和具体计划后继续执行；只有影响结果、契约、归属或范围的实质选择需要询问。用户只要求分析、设计或计划时，交付对应结果，不进入实施。

```text
需求 → using-cortex 路由
     → 按需 code-tracing / systematic-debugging
     → 有实质未知项时 brainstorming 澄清
     → 行为改动先 writing-plans 明确如何实施
     → executing-plans 直接执行；用户指定时使用 subagent-driven-development
     → code-standards / requesting-code-review
     → verification-before-completion
```

#### 技能入口

| 技能 | 职责 |
| --- | --- |
| [using-cortex](plugins/cortex-se/skills/using-cortex/SKILL.md) | 请求路由、来源证据、范围与完成要求 |
| [brainstorming](plugins/cortex-se/skills/brainstorming/SKILL.md) | 处理影响结果的未知项和设计选择 |
| [code-tracing](plugins/cortex-se/skills/code-tracing/SKILL.md) | 追踪与当前结论有关的源码路径 |
| [writing-plans](plugins/cortex-se/skills/writing-plans/SKILL.md) | 明确行为改动的实施路径，或交付用户请求的计划 |
| [executing-plans](plugins/cortex-se/skills/executing-plans/SKILL.md) | 执行已获授权的工作 |
| [subagent-driven-development](plugins/cortex-se/skills/subagent-driven-development/SKILL.md) | 用户要求时协调委派实施 |
| [code-standards](plugins/cortex-se/skills/code-standards/SKILL.md) | 生成或编辑代码的质量要求 |
| [systematic-debugging](plugins/cortex-se/skills/systematic-debugging/SKILL.md) | 证明观察到的失败根因 |
| [requesting-code-review](plugins/cortex-se/skills/requesting-code-review/SKILL.md) | 基于源码证据审查改动 |
| [verification-before-completion](plugins/cortex-se/skills/verification-before-completion/SKILL.md) | 用当前验证证据报告结果和缺口 |

## 通用插件验证流程

本节适用于本仓库的两个插件，也可作为其他插件的验证指南。在仓库根目录选择待验证插件后，进入该插件目录，再运行下面的组件校验示例：

```bash
cd plugins/cortex
# 验证 Cortex SE 时，改为 cd plugins/cortex-se
```

本节是新建或修改 Codex 插件时可复用的验证指南，适用于纯 Skills 插件及包含 MCP、Apps、Hooks、Scripts 或 Assets 的插件。根据插件声明的实际组件选择检查项；不要求插件采用某一种工程流程或固定数量的技能。

```text
定义能力与验收标准
  → 确定插件格式及实际组件
  → 结构、路径、依赖校验
  → 独立子会话审查 + 主会话复核
  → 按组件进行功能验证
  → 本地安装/刷新 → 版本与安装资源核对
  → 新会话使用安装版进行端到端行为验证
  → 汇总全部结果和未完成项
  → 最终报告保存必要证据摘要
  → 清理过程产物并核对结果
```

### 完整验证的验收条件

新建或修改插件后请求“验证插件”，默认执行本节完整流程。不能由执行者自行缩小为源码检查、脚本检查或工具结构校验，也不能因为换了会话而省略步骤。

独立子会话审查、本地安装及资源核对、新会话中的安装版行为验证均是必需阶段。已安装版本与待验证源码一致时，可核验现有安装；否则应安装或刷新后再测试。组件未声明可记为不适用，但不能把必需阶段标为不适用。

开始前明确所需工具、目标宿主、安装来源和测试环境。若子会话工具、安装权限、Hook 信任、外部依赖或目标宿主能力不可用，应记录阻塞和具体缺口，报告“完整验证未完成”，而不是改称另一种验证范围或笼统报告通过。外部写入和不可逆操作仍须其自身授权。

### 等待与无进展处理

以下规则适用于所有验证阶段的命令、子会话、子代理和其他异步任务：

- 等待开始时记录任务标识、当前阶段、预期结果和最近一次有效进展时间；每 30 秒检查一次实际进展，单次阻塞等待不得超过 30 秒。
- 有效进展必须能对应验收目标，例如任务阶段推进、新结果完成或产物内容发生有意义的变化。进程存活、心跳、空轮询、重复日志、“仍在等待”以及仅更新时间戳不算有效进展，不得重置无进展计时。
- 连续 120 秒无有效进展，停止继续盲等，转入排查：核实任务是否实际派发、工具是否返回、是否存在阻塞，以及实际进程和子进程状态。无终端输出或等待事件未展示目标标识，均不能单独证明任务卡死或未派发；必须结合实际记录和结果判断。
- 仅在阻塞已处理，或已有证据表明任务正在产生有效进展时恢复等待。保留既有批准和已完成步骤，不重复派发完成任务，也不重复执行相同的空等待。
- 需要终止时，先明确要终止的任务和进程范围；发出终止指令后核实实际进程及子进程状态，并检查结果和产物是否仍在更新。控制工具返回、停止信号已发送或控制会话退出，不能单独作为实际任务已停止的证据。
- 向用户报告无进展后的排查结果或阻塞原因，不用重复状态消息代替实际进展。确实无法继续时，将对应验收项记录为“未完成”并保留具体缺口；只有实际结果证明不满足验收标准时才记为“失败”，不得因超时缩小验证范围或报告通过。

### 1. 定义验证范围

先列出插件解决的用户问题、支持与不支持的能力、目标宿主和运行环境。为每项能力定义可观察的成功结果、必要输入、依赖及错误行为，再选择相应工具和场景。

新插件应验证所有声明的组件及组合路径。修改既有插件时，验证改动部分及受影响的组件交接，同时检查插件整体的发现、加载和关键入口；不要将其他人的改动混入本次结果。

若目录属于 Git 仓库，在插件根目录检查工作树和暂存区；非 Git 项目可使用文件清单与版本记录确定范围。

```bash
git status --short -- .
git diff --stat -- .
git diff --cached --stat -- .
```

### 2. 确定格式并校验结构

先选择目标平台支持的规范，不混用不同格式的字段与校验工具：

- Portable Agent Plugins 包使用根目录 `plugin.json`，按声明的 `$schema` 校验；可包含根目录 `skills/`、`mcp.json` 及资源。
- Codex 兼容格式使用 `.codex-plugin/plugin.json`，可声明 `.mcp.json`、`.app.json`、技能及其他支持的资源。根目录存在已识别的 portable 清单时，应先检查平台对清单和兼容层的优先级规则。
- 检查名称、版本、描述、作者及目标平台要求的元数据；检查 JSON/YAML、目录层级、资源引用、相对路径和依赖声明。
- 所有声明的文件必须存在且可读。脚本所需的解释器、库、网络及认证条件应明确；模板占位符不能冒充完整配置。
- 未声明或不支持的组件应标为不适用，不能要求每个插件都具有 Skills、MCP 或 Hooks。

官方 `plugin-creator` 辅助校验器与通用 JSON Schema 校验并非同一覆盖范围。使用前确认当前工具支持的包格式；不能用兼容层通过的结果证明 portable 清单或公开发布审核也通过。

### 3. 执行适用的工具校验

以下示例从待验证插件根目录运行，使用 Bash（Windows 可用 Git Bash）。Python 版本及其他依赖以实际工具要求为准；Windows 可设置 `PYTHONUTF8=1` 读取 UTF-8 文件。工具不在下列目录时，调整变量；社区 `skills-ref` 需要已安装并位于 PATH 或已激活其虚拟环境。

```bash
plugin_root="$PWD"
plugin_codex_dir="${CODEX_HOME:-$HOME/.codex}"
plugin_skill_creator="$plugin_codex_dir/skills/.system/skill-creator"
plugin_plugin_creator="$plugin_codex_dir/skills/.system/plugin-creator"
```

**清单校验。** 根据格式选择入口。下面对 portable 格式只演示 JSON 语法检查：还必须使用匹配其 `$schema` 的 JSON Schema 校验器完成规范校验；未执行时应记录缺口。当前 `plugin-creator` 的本地辅助脚本以 `.codex-plugin/plugin.json` 为入口。

```bash
if test -f "$plugin_root/plugin.json"; then
  PYTHONUTF8=1 python -m json.tool "$plugin_root/plugin.json" > /dev/null || exit 1
  printf '%s\n' 'JSON syntax passed; validate the declared portable schema separately.'
elif test -f "$plugin_root/.codex-plugin/plugin.json"; then
  PYTHONUTF8=1 python "$plugin_plugin_creator/scripts/validate_plugin.py" "$plugin_root" || exit 1
else
  printf '%s\n' 'No supported plugin manifest found.' >&2
  exit 1
fi
```

**技能校验。** 插件有 Skills 时，逐个校验实际声明或按规范发现的技能，而不是只校验修改过的技能。以下示例使用默认 `skills/`；兼容清单若指定其他目录，应按实际声明调整。没有技能是“不适用”，不是技能校验通过。

```bash
plugin_skill_count=0
for plugin_skill in "$plugin_root"/skills/*; do
  test -f "$plugin_skill/SKILL.md" || continue
  plugin_skill_count=$((plugin_skill_count + 1))
  PYTHONUTF8=1 python "$plugin_skill_creator/scripts/quick_validate.py" "$plugin_skill" || exit 1
  PYTHONUTF8=1 skills-ref validate "$plugin_skill" || exit 1
done
if test "$plugin_skill_count" -eq 0; then
  printf '%s\n' 'Skill validation: not applicable, or verify a custom declared skill path.'
fi
```

**改动格式检查。** 对 Git 项目分别检查已暂存和未暂存差异。范围外问题单独归属，不擅自修改，也不将失败的全树检查报告为通过。

```bash
git diff --check -- . || exit 1
git diff --cached --check -- . || exit 1
```

工具检查只覆盖其支持的格式、配置或结构，不能证明指令语义正确、功能可用、模型实际遵循，或发布审核已经通过。

### 4. 独立子会话审查与主会话复核

启动新的、只读子会话进行独立审查。给它待验证插件路径、声明能力、验收标准和范围，不传递主会话的预设结论；子会话不得再次派生审查者或修改源码。保留子会话标识及审查结果。

主会话须复核每项结论引用的实际文件、调用路径和影响，处理已证明的问题；子会话一句“通过”不能代替这次复核。子会话不可用时，这一阶段是阻塞项，不能直接省略。

对于技能、工具描述、Hook 和引用资料，检查：

- 用户目标、触发条件、输入、步骤、输出及停止条件是否清晰；显式用户指令的优先级是否明确。
- 是否存在无意义重复、语义冲突、前后矛盾、循环交接或缺失前置条件。
- 多技能或多工具是否职责重叠，是否能从一个组件的结果正确进入下一组件。
- 引用、元数据和依赖是否有效；示例是否与真实能力、参数和结果一致。
- 授权边界是否与实际操作匹配；缺少输入或依赖时是否会编造结果或执行未经授权的操作。
- 正文是否只包含与该能力有关的约束，详细资料是否按需引用，避免将某个具体插件的工作流当成所有插件的必备流程。

每个问题记录位置、触发条件、影响和证据。文本重复不一定是缺陷，必要的独立入口约束可保留；证据不足的问题应列为开放问题，不冒充已证明的故障。

### 5. 按实际组件进行功能验证

| 组件 | 检查内容 |
| --- | --- |
| Skills | 是否被正确发现；应触发和不应触发的请求；资源加载、步骤及输出是否符合该技能自身的约定 |
| MCP / Apps | 连接与发现、认证、参数和结果 schema、工具选择、读写边界、空结果及错误返回；需要时使用 MCP Inspector 直接调用工具 |
| Hooks | 实际声明的事件是否触发；参数、环境、依赖、超时、退出状态及失败处理是否正确；平台要求的信任设置是否完成 |
| Scripts | 依赖是否齐全；代表性输入、边界和错误输入的结果是否正确；文件或外部服务副作用是否在约定范围内 |
| Assets / UI | 引用文件能否加载；内容与工具结果是否一致；有 UI 时检查渲染、错误和状态恢复 |

先验证组件，再验证完整插件。组件不存在时标为不适用。外部访问、写入或破坏性操作应在已授权范围及合适测试环境中验证，不能因“做校验”自动获得权限。

代码测试根据组件的实际契约和改动选择有验证价值的用例，优先复用相关既有测试；不要用无关测试通过代替当前能力验证，也不要为了凑校验数量创建恒真断言或复述实现的测试。

### 6. 本地安装与版本核对（有副作用）

新插件先按目标宿主支持的方式配置本地源码和 marketplace，再安装。修改既有插件时，先验证源码，再按其包格式和工具支持情况更新版本或缓存标记。版本更新和安装会改变文件及本地状态，只在相关操作已获授权时执行。

Codex 本地 marketplace 安装示例：将名称替换为已经配置并校验过的实际名称，不复制某个具体插件的标识。

```bash
plugin_name='my-plugin'
plugin_marketplace='my-marketplace'
codex plugin add "${plugin_name}@${plugin_marketplace}"
codex plugin list
```

对于已有 Codex 兼容格式的本地插件，可以使用 `plugin-creator` 的辅助流程。下列命令是有副作用的示例，不属于只读结构校验；portable 包不能假设该兼容格式辅助脚本适用。

```bash
plugin_marketplace_path="$(cd "$plugin_root/../.." && pwd)/.agents/plugins/marketplace.json"
plugin_marketplace=$(PYTHONUTF8=1 python "$plugin_plugin_creator/scripts/read_marketplace_name.py" --marketplace-path "$plugin_marketplace_path") || exit 1
PYTHONUTF8=1 python "$plugin_plugin_creator/scripts/update_plugin_cachebuster.py" "$plugin_root" || exit 1
PYTHONUTF8=1 python "$plugin_plugin_creator/scripts/validate_plugin.py" "$plugin_root" || exit 1
```

上面的示例使用本仓库的市场配置位置；其他插件应按实际目录设置 `plugin_marketplace_path`。非默认 marketplace 应向辅助脚本提供其实际路径，并确认安装来源确实指向待验证源码。更新后重新检查清单及 diff，再执行已授权的安装；不要手工修改 marketplace 配置来掩盖来源不一致。

安装完成后核对：

- 插件名、来源、版本及安装/启用状态与预期一致。
- 安装结果包含实际声明的技能、脚本、Hook、服务配置和资源，引用可被加载。
- 原样复制的本地资源可比较文件内容；经过生成或格式转换的资源按打包映射检查，不能一律要求源文件和转换产物字节相同。
- 在新任务中验证发现、触发、组件功能和端到端结果；更新服务元数据后按宿主要求刷新连接。

安装缓存存在、配置文件存在或版本一致，不足以证明所有组件已经运行成功。

### 7. 新会话验证安装版的端到端行为

完成前一阶段安装及资源核对后，启动新的验证会话，在目标宿主中启用安装版插件并运行代表性请求。使用安装路径，不能继续读取开发源码就当作安装版测试；原会话的静态推演不能代替此阶段。保留新会话标识、提示词、实际选择的技能/工具、参数、结果和错误，以便后续版本比较。

新会话与只读审查子会话职责不同：前者执行安装版的行为案例，后者独立检查源码和指令。使用支持独立上下文的子会话或新任务机制，不能只在原上下文中口头假设已切换。

| 场景 | 验证目标 |
| --- | --- |
| 直接请求某项声明能力 | 使用正确组件并完成约定结果 |
| 用不同表达表达同一目标 | 仍能正确识别能力，不依赖唯一关键词 |
| 缺少必要输入 | 按约定询问或返回明确错误，不编造输入 |
| 不属于插件能力的请求 | 不误触发或扩大职责 |
| 边界输入、空结果、依赖不可用 | 按约定处理，不伪装为成功 |
| 需要先前结果的跟进请求 | 正确传递标识、状态和上下文 |
| 涉及写入、权限或不可逆操作 | 授权与实际操作范围一致 |
| 多个组件协作 | 交接数据、执行顺序和输出语义一致 |

插件声明的特定业务规则应转化为该插件自己的测试场景，不作为通用插件校验标准。静态审查、场景推演和实际模型/组件调用分别记录；没有真实运行就不能报告运行时行为已验证。

### 8. 记录结果与回归范围

| 状态 | 记录要求 |
| --- | --- |
| 通过 | 明确验证对象、命令/请求及实际证据 |
| 失败 | 保留退出状态、错误、触发条件及影响 |
| 未执行 / 不适用 | 说明组件不存在、工具缺失、环境限制或未授权范围 |
| 证据不足 | 说明尚不能证明的关联或结果以及所需证据 |

逐项记录结构工具、独立子会话审查、主会话复核、组件功能、本地安装、资源核对和新会话行为验证的结果与证据。只有必需阶段均完成、没有未解决的阻断问题且过程产物清理核对完成，才可报告“完整验证通过”。任一阶段未执行、失败或证据不足，应报告“完整验证未完成”并列出缺口；任一工具的成功不能代替其他阶段。

修改名称、描述、schema、认证、Hook、资源或组件交接后，重跑受影响的案例，并检查整体发现、加载和关键入口。保存旧版与新版的实际结果，定位差异；没有执行的项目必须保留在报告中，不能用“插件通过验证”概括尚未验证的能力。

### 9. 过程产物清理

所有插件校验均应完成此收尾步骤：

- 创建产物时集中使用自有临时目录，记录路径、归属及用途；将临时文件与源码、用户既有文件、实际安装资源和正式工具分开。
- 校验结束且相关任务及实际进程不再使用产物后，将插件版本、校验工具和命令、会话标识、请求与结果、失败或缺口、必要输入输出摘要及清理结果归入最终报告。保留最终报告；临时日志、快照和报告草稿不作为永久交付物。未完成或失败的校验也应先记录实际状态，再清理已停止任务的产物。
- 删除本次创建的全部过程产物，包括隔离测试项目、临时脚本和配置、原始日志、查询清单、模拟数据与临时证据库、子代理恢复记录、临时依赖环境及下载包。需保留的复用工具必须明确为正式工具；仅因未来可能使用，不得默认保留临时目录中的工具环境。宿主管理的原生会话历史、用户既有数据及实际安装插件不作为临时文件擅自删除。
- 删除前核实目标的解析后绝对路径及归属，确认其位于已识别的临时目录内；外置临时证据库只能按已核实的隔离项目关联逐个删除，不得清空共享数据目录。只删除清单内产物；无归属依据的文件保持原样并记录。
- 清理后重新检查目录、文件及外置临时记录是否仍有残留，更新最终报告中的临时路径引用，避免留下失效链接。报告实际清理范围和结果；失败或残留应明确列出，不能只凭删除命令返回成功宣称清理完成。

### 参考规范与工具范围

- [OpenAI：Package your plugin](https://developers.openai.com/plugins/build/plugins)：包格式、路径、组件声明及本地来源。
- [OpenAI：Build skills](https://developers.openai.com/plugins/build/skills)：技能编写、依赖及代表性请求。
- [OpenAI：Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt)：先验证组件，再验证完整插件。
- [Agent Skills 规范](https://agentskills.io/specification)及 [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref)：技能格式和参考校验器。

以目标平台当前规范和当前工具实际支持的范围为准；本指南不代表公开插件目录的审核或认证。
