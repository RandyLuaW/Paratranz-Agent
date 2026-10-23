# WebUI 控制台

浏览器里填 Token、看进度、给 Agent 下指令。不用记命令行。


---

## 一、启动

```bat
cd paratranz-agent
web.bat
```

浏览器自动打开 `http://127.0.0.1:8787`。关掉那个命令行窗口（或 Ctrl+C）即停止。

**没有 Token 也想看界面？**

```bat
web.bat --mock
```

用内置假平台（`fake.py`），完整演示分派/翻译/校对/术语/规范，不碰真实项目。界面右上角会有 `DEMO` 标记。

> `--mock` 只模拟 **ParaTranz 平台**，不模拟模型。翻译/校对仍需要真实的模型 API——
> 产品里没有「无 key 也能跑」的降级。想跑通全流程，去「模型」页填一个可用的 API。

### 深色 / 浅色主题

侧栏底部有「☾ 深色 / ☀ 浅色」切换按钮。选择存在浏览器的 `localStorage`（键 `ptz-theme`），
**没有存过时跟随系统的 `prefers-color-scheme`**。`index.html` 的 `<head>` 里有一段内联脚本，
在首帧之前就把主题定好，所以刷新不会闪一下深色。

两套主题只换颜色、不动布局：41 个 CSS 变量里，尺寸类（`--radius` `--mono` `--side`）只在 `:root`
定义，浅色块只覆盖颜色。浅色取值都按对比度选过（正文 15.3:1、次要文字 5.96:1、
状态色 ≥ 4.6:1），并给两套都设了 `color-scheme`，滚动条与原生控件跟着走。

---

## 二、第一次用：三步

1. 左侧「设置」→ 填 **项目编号**（例如 `6896`）和 **API Token**
   （Token 在 <https://paratranz.cn/users/my> 的「设置」标签页生成；账号需在项目里是管理员）
2. 点「测试连接」→ 看到项目名和成员数就通了；有警告会标黄（比如任务功能未开启）
3. 回「概览」→ 在输入框里说一句话，点「执行」

---

## 三、页面

界面按 **工作台 / 配置 / 观测 / 扩展** 四组组织侧栏，每页顶部有面包屑与页头（标题 + 一行关键数字），
主体优先用紧凑的「指标条 + 表格」而不是大块留白的卡片。

### 概览

两栏布局，一屏看全：

- **指标条**（一行 10 个）：已翻译 / 进度 / 待翻译 / 已检查 / 已审核 / 有疑问 / 文件 / 词数 / 术语 / 本地 TM
- **左栏**：目录进度表（按目录聚合，与平台文件页同构）+ 最近任务（点开看日志与结果）
- **右栏**：给 Agent 下指令（自然语言 + 快捷按钮）+ 状态速览（演练模式、模型 API、平台任务、TM、术语）

### 任务


每个任务一行，状态用颜色区分（queued / running / done / failed / cancelled）。点标题展开：


- 结果 JSON（各 pipeline 的统计）
- 逐步日志（含「演练模式：仅预览，未写入平台」这类提示）
- 运行中的任务有「取消」按钮

### 文件

按待翻译量降序的表格，每行带进度条。一眼看出该先啃哪个文件。

### 提取


把游戏文件里的词条抽出来，再导入平台。**提取器**是可插拔单元，一个目录一个提取器——
内置三个，也可以让 Agent 读懂你的游戏目录，当场写一个 / 改进一个。

内置的三个：

| 提取器 | 覆盖 |
| --- | --- |
| 通用 JSON | 递归遍历 JSON，按路径模式提取（data/*.json、locale/*.json、i18n） |
| 通用正则 | 扫文本按正则抓（Ren'Py 的 .rpy、自定义 txt/csv） |
| RPG Maker MV/MZ | data/*.json 数据库字段 + 地图/公共事件对话（code 401/405）+ System 标题与术语 |

右侧参数表单从 `extractor.yaml` 的 `args` **动态生成**，不用改前端就能加新参数。

「分析目录」会粗扫游戏目录，给出引擎猜测、扩展名分布、命中特征和文件样例——
用来确认该选哪个提取器，也可以作为让 Agent 写新提取脚本的输入。

**提取结果**：


- 统计：总条数 / 错误数 / 警告数
- 错误与警告可展开（带行号、规则名、key）
- 词条表：key / 原文 / 上下文
- 「复制 JSON」导出平台 items；「导入平台」选目标文件后提交

**校验规则**（`validate`）：

| 级别 | 规则 |
| --- | --- |
| 错误 | `empty_key`、`not_string`、`empty_original`、`dup_key` |
| 警告 | `too_long`（>5000 字符）、`placeholder_only`（去掉占位符没内容）、`no_context`、`key_format`（不含 `::` 或 `/`）、`too_many`（>20 万条） |

有错误时导入会被拦下，除非勾「强制导入」。

**执行提取器 = 在本机跑任意代码**


提取器是你提供的脚本，它会以**当前用户权限**在本机运行——能读写任意文件、访问网络、执行系统命令。
所以：

- 提取页顶部常驻警示条
- 点「开始提取」会弹确认框，**必须等 5 秒倒计时结束 + 勾选「我已了解上述风险」**，按钮才可点
- 后端 `/api/extractors/{id}/run` 要求 body 带 `accept_risk: true`，否则 400（前端绕不过去）
- 执行后果由使用者自行承担

**游戏目录限制**：拒绝盘符根（`C:\`）、系统目录（`Windows` / `Program Files` / `ProgramData` 等）、
用户主目录根。提交时就校验，不会等 job 跑起来才报错。

**导入时的平台语义（重要）**

ParaTranz 有两套写入接口，别用错：

| 接口 | 能做什么 | 不能做什么 |
| --- | --- | --- |
| `POST /files/{fileId}/translation` | 更新**已存在**词条 | **不会创建新词条**（新 key 被静默忽略） |
| `POST /projects/{id}/strings` | 创建**一条**新词条（需管理员） | 没有批量接口 |

所以「导入」不能只调 translation：提取出来的都是新 key，会被全部丢掉。
现在的流程是：

1. 拉目标文件已有的全部 key
2. 算差集 —— 已存在的跳过（平台里可能已有人工译文，不覆盖）
3. 对缺失的逐条 `POST /strings` 创建（受 480 次/分限流保护，走 job 能看到进度）
4. 结果报「新增 N / 跳过 M / 失败 K」

几千条会比较慢（1000 条约 2 分钟），这是平台没有批量创建接口导致的。

**导入预览**


导入对话框有「仅预览」按钮：调 `/import` 带 `dry_run: true`，
返回**将新增多少条、已存在多少条**（会实时查平台）与前 20 条样表，**不写平台**。

> 另外平台支持 `POST /files`（multipart 上传文件创建词条），但要求文件名不与目标目录下已有文件冲突，
> 不能覆盖同名文件，所以不适合「往已有文件里补词条」这个场景。

**提取器分两组显示**

左栏按来源分组。来源靠 `.ptz-state/extractors/<id>/.agent` 标记文件区分，
`extractor.yaml` 里写了 `builtin: true` 的算内置：

| 分组 | 来源 | 卡片标签 |
| --- | --- | --- |
| Agent 生成 | `source=agent`（目录里有 `.agent` 标记） | 「Agent」 |
| 导入 / 内置 | `source=imported` / `source=builtin` | 内置的标「内置」 |

`GET /api/extractors` 返回的每个提取器都带 `source` 字段。


**🤖 让 Agent 写一个**

左栏顶部的按钮。填游戏目录就行，也可以加要求（例如「只抽对话，不要物品名；编码是 GBK」）。
点「开始生成」后会弹风险确认——和「开始提取」同一套 5 秒倒计时 + 勾选，
因为生成完会**立刻试跑一次**，等于执行代码。

后端是一个 job，四步：

| 步 | 做什么 |
| --- | --- |
| 1 | `analyze_game_dir` 画像目录：扩展名分布 / 引擎猜测 / 命中特征 / 文件样例 |
| 2 | `sample_files` 采样 5-6 个文件的内容片段：按扩展名分组每类取一个，每段最多 1200 字符 |
| 3 | 画像 + 样例 + 你的要求一起喂给模型，要求输出 JSON：`{id, name, engine, desc, input_hint, args[], code}` |
| 4 | 语法预检 `compile()` → 安全护栏 → 落盘 → 自动试跑（`args` 用默认值） |

落盘到 `.ptz-state/extractors/<id>/`：`extractor.yaml` + `run.py` + `.agent` 标记。
下次打开提取页，它就在「Agent 生成」组里，参数表单照样从 `args` 动态生成。

**安全护栏**（生成时静态检查，过不去就不落盘）：

| 拦住什么 | 具体 |
| --- | --- |
| 模块导入 | `subprocess` / `socket` / `ctypes` / `requests` / `urllib` / `multiprocessing`（`import os, subprocess` 这种逗号导入也拦） |
| 危险调用 | `os.system` / `os.popen` / `eval` / `exec` / `__import__` / 删文件的 `os.remove`、`os.unlink`、`os.rmdir`、`shutil.rmtree` |

失败原因会原样贴出来：护栏给具体条目（如「生成的脚本超出提取器能力边界：subprocess（超出提取器能力边界）」）、
语法错误带行号、试跑失败把错误信息整段抛给你。

**结果弹窗**：提取器 id / 试跑条数与耗时 / 校验错误数与警告数 / 前 8 条样表 /
「查看全部词条」（直接跳到提取结果页）。


**🤖 让 Agent 改进**

每张提取器详情卡右上角。内置的也有这个按钮——**改进会覆写内置脚本**，谨慎。

填游戏目录 + 想改进什么（可选，例如「漏了 Map 里的对话；把纯数字过滤掉」），点「开始改进」后同样先过风险确认。
后端把这四样一起给模型：

- 现有 `extractor.yaml` + 现有 `run.py`
- 上次运行日志（末尾 60 行）
- 目录画像 + 文件样例
- 你的反馈

两条硬规矩：

- **强制沿用原 id**：模型想改名也不改，否则任务路由和历史记录会断
- **先备份**旧脚本到 `<提取器目录>/.backup/run-<时间戳>.py`，再覆写

之后同样自动试跑，返回的结果结构和生成时一致。

**接口**

```
POST /api/extractors/generate         {game_dir, hint?, accept_risk: true}      → {job}
POST /api/extractors/{eid}/improve    {game_dir, feedback?, accept_risk: true}  → {job}
GET  /api/extractors                  每个提取器带 source 字段
```

`accept_risk` 必填，否则 400——生成后会立即试跑，等于执行代码，前端绕不过去。

**闸门在工具层，所以三条入口都绕不过去**：路由（上面两个 400）、通用任务口
`POST /api/jobs {tool, args}`、以及 `/api/agent`。后两条以前能绕过——审计实测改走
通用口提取器照跑、任意代码照执行。现在 `extract` / `extractor_generate` /
`extractor_improve` 在工具函数里各自再校验一次 `accept_risk`，缺了直接 `PermissionError`。

**Agent 也不会替你执行危险动作**：`/api/agent` 遇到这三个工具时不再提交任务，
而是放在返回体的 `needs_confirm` 里，界面上提示「需到对应页面勾选风险确认后再执行」。
原因很实在——模型只是把自然语言映射成工具序列，而输入可能来自游戏文件内容（提示注入），
审计实测「忽略之前规则」这类输入能让它真的执行本机代码。

**模型与提示词**：任务路由新增 `extractor`（「提取脚本生成」），建议单独指一个**代码能力强的模型**；
提示词页也多出「提取脚本生成」这一环节，可以自定义它的 system prompt。

**提取器协议**（自己写一个）：

```
my-extractor/
  extractor.yaml    # id / name / engine / entry / args[]
  run.py            # 入口
```

运行时给 `--input <游戏目录> --output <输出.jsonl> --opt k=v`，
同时注入环境变量 `PTZ_INPUT` / `PTZ_OUTPUT` / `PTZ_OPTIONS`。
输出 JSONL，每行 `{"key","original","context"}`，key 全局唯一。

任何语言都能写，只要按协议写文件。

### 回写译文（独立选项卡）

侧栏「工作台 → 回写」是一个**一级页面**——这是最终产出汉化成品的一步，所以单列管理：
顶部一行摘要（可回写的提取记录数 / 上次回写时间 / 默认输出目录），中间是提取记录表
（只列 `done` 且记录过游戏目录的记录），选中一条就在下方展开参数区；
进页面会自动选最新一条，并先渲染上次的报告。

「提取」页的记录行仍保留一个「回写译文」按钮，点了会**跳到回写页并选中该记录**；
提取详情面板里的按钮则打开**就地弹窗**（看完词条顺手发一次，不用离开当前页）。
弹窗与页面共用同一套参数区实现（`wbParamsHtml` / `wbWire`），行为不会漂移。

它做的事和提取正好相反：**把平台上确认过的译文写回游戏文件的原始位置**。

参数区有四项：

| 控件 | 默认 | 说明 |
| --- | --- | --- |
| 译文门槛 `#wb_stage` | 设置页的默认（初始「已检查 3」） | 只回写达到该 stage 的词条；「已审核(5)」最严 |
| 回写形式 `#wb_mode` | 设置页的默认（初始 `整包镜像`） | **整包镜像**：目标目录是一份完整可跑的游戏（未改动文件原样复制，改动文件直接写改写版）；**仅改动文件**：只放被改动的那几个 |
| 输出目录 `#wb_out` | 设置页的默认（初始空） | 留空 = 游戏目录旁的 `<游戏目录>_cn`；这里改只影响这一次 |
| 仅预览 `#wb_dry` | **勾选** | 跑一遍、出报告，但一个文件都不写 |
| 原地修改 `#wb_inplace` | **不勾** | 勾上会出现醒目警示 + 5 秒倒计时 + 必须勾「我已了解风险」才能提交 |

**默认值在「项目」设置页**（`writeback_out_dir` / `writeback_mode` / `writeback_min_stage`）：
把汉化版输出目录固定成一个路径（例如 `D:/Games/我的游戏_CN`），之后每次回写都往那里产出。
弹窗里改只覆盖本次，底部会提示「（来自设置页默认值）」。

为什么默认不原地改：回写会覆盖游戏文件，万一 key 定位错了就是数据损坏。
默认产出一份**完整可跑的游戏目录**（整包镜像），你直接用它替换或启动，风险最小。

⚠️ 输出目录**不能放在游戏目录里面**——镜像会把游戏目录整份复制过去，若输出目录在其内部
就等于把自己当素材，目录翻倍甚至递归写满磁盘。这条在后端硬拦（400）。

**原文校验**：写回前会逐条比对「文件里这段现在写的什么」与「平台记录的原文」。
游戏本体更新过之后行号/路径会整体错位，对不上就跳过并在报告里记 `mismatched`，
绝不硬写。报告还会给出 `missing`（定位符找不到）、`unsupported`（没有写回器认这个定位符）、
`copied`（整包镜像里原样复制的文件数）、`skipped_stage` / `skipped_empty` /
`skipped_no_original` 等分项计数。

**原地改的备份**：备份放在 `<state_dir>/writeback/<提取记录id>/backup-<时间戳>/`，同时写
`manifest.json`（每文件的备份路径 + 改动前 sha256）。备份**故意不放进游戏目录**——
放进去了，下次提取的 `**/*.json` 会把备份再扫一遍变成新词条。

接口：`POST /api/extractions/{jid}/writeback`（起 job）、
`GET /api/extractions/{jid}/writeback/last`（最近一次报告，返回 `{saved_at, report}`）。

### 工具


每个 pipeline 一张卡，标了「读」/「写」、说明、对应的平台接口。点「运行」直接执行。

### 项目

分四组：ParaTranz / 模型 / 翻译 / 校对与任务。

- **Token 和 API Key 只写不回显**：输入框永远空白，占位符显示「已配置」或尾号。回传脱敏值不会覆盖真值。
- **模型配置在「模型」页**：项目页只管 ParaTranz 连接与翻译/校对策略，不重复配模型。

---


### 模型（源 → 模型 → 任务路由）


模型配置**只在这一页**。三层结构：

```
提供商源（base_url + key + 超时/代理）
  └── 模型（id + 能力 + 参数覆盖 + 上下文窗口 + 启用）
```

一个源可以聚合多个模型（中转站、聚合网关都这样）。

**源配置**：显示名 / API Base URL / API Key / 超时 / 并发 / 重试 / 代理 / 备注。
左侧列表用状态点区分（绿=有 Key、黄=缺 Key），标签数字是模型数与被引用任务数。

**模型列表**：


每行显示：模型名 + ID + 能力标签 + 上下文长度 + **连接状态** + 开关 / 测试 / 设置 / 删。

| 按钮 | 作用 |
| --- | --- |
| 获取模型列表 | 调 `GET {base_url}/models` 自动探测，勾选后批量添加（已配置的会置灰） |
| + 自定义模型 | 手动加（源不支持 /models 时用） |
| 检查全部 | 并发 3 个，逐个测，结果填到「连接」列 |

**探测面板**：


**测试结果**：每行「连接」列显示绿色 `20ms` 或红色「不可用」（hover 看原因），
单测和全测都会弹 toast。


**模型参数（独立于源）**：


- 模型 ID / 显示名
- **能力**：文本 / 图像 / 音频 / 工具使用
- 上下文窗口
- temperature / max_tokens（留空继承源）
- **自定义请求体参数**（JSON，如 `{"top_p":0.9}`，直接合并进请求体）

**任务路由**：


| 任务 | 说明 |
| --- | --- |
| `agent` | Agent 意图解析（把一句话拆成工具调用） |
| `translate` | 翻译 |
| `review` | 校对 |
| `glossary` | 术语抽取 |
| `styleguide` | 规范生成 |
| `extractor` | 提取脚本生成（写 / 改提取器） |

路由指向 `provider_id::model_id`。翻译/校对上强模型，意图解析用便宜模型，成本能差一个数量级；
`extractor` 建议指到代码能力强的模型。
底部是默认模型与回退链。

### 数据


8 个统计卡（今日调用 / Tokens / 花费 / 本月花费 / 累计 Tokens / 累计调用 / 缓存命中 / 累计花费）+
每日花费条形图（近 14 天）+ **按模型** / **按工具** / **按任务**三个维度排名。

按任务的表格能直接回答"这次 translate 花了多少钱"。

### 日志


- 按**级别**（debug/info/warn/error）、**关键字**、**任务 ID** 筛选
- 实时显示缓冲条数与各级别计数
- 按天落盘的日志文件列表，可下载全文（默认最后 3000 行）
- 「清空缓冲」只清内存，磁盘文件保留

后端实现（`server/logs.py`）：内存环形缓冲 3000 条 + 按天写 `logs/agent-YYYY-MM-DD.log`，
单文件超 8MB 自动滚动。文件名做了白名单校验，防路径穿越。

### 开销


成本统计按汉化场景裁剪：逐次调用记账 + 四维预算（单次 / 单任务 / 每日 / 每月）。

**四维预算护栏**（每个维度都能设 Token 上限与美元上限，任一触发即执行动作）：

| 维度 | 适合场景 |
| --- | --- |
| 单次任务 | 一次 translate/review 跑飞最烧钱，建议设紧 |
| 每日 | 当天所有任务累计 |
| 单模型每日 | 单独约束高价模型 |
| 每月 | 最终月度上限 |

**三种动作**：`仅告警` / `硬拦截`（任务开始前就拒绝执行）/ `切备用`（提示切换到备用模型）。

> 诚实的限制：`切备用` 目前只告警并提示备用模型名，**不自动切换**——动态换模型需要重建
> HTTP 连接与定价上下文，在任务中途换会污染用量归因，所以留作后续。

**定价**：内置 17 个常见模型（OpenAI / Anthropic / DeepSeek / Qwen / GLM），
单位 USD/百万 token，四个档位（输入 / 缓存命中 / 输出 / 缓存写入）。
模型名匹配走「精确 > 前缀 > 子串，最长优先」，所以 `gpt-4o-mini-2024-07-18` 自动命中 `gpt-4o-mini`。
可以在页面下方直接覆盖或新增。

**最近调用明细**：时间 / 模型 / 工具 / 任务 / 输入输出 tokens / 花费 / 耗时。

后端实现（`server/usage.py`）：
- 每次模型调用后由 `llm.on_usage` 回调记账，通过 `contextvars` 归属到当前 job；
- 单表 `usage`，按 job / 天 / 模型索引；
- 预算事件单独记 `budget_event` 表，页面能看到"什么时候因为什么被拦了"。

### 插件与技能


三个 tab：插件 / SKILL / MCP，带搜索。卡片结构：

- 左侧首字母图标块
- 名称 + 版本 + 启用状态标签
- 能力标签（类型 / 作者 / 「运行引擎待接入」）
- 两行描述（超出截断）
- 底部路径 + 移除按钮，右上角启停开关

| 类型 | 目录约定 | 说明 |
| --- | --- | --- |
| 插件 | `.ptz-state/plugins/<id>/metadata.yaml` | metadata 里声明元信息与挂载页面 |
| SKILL | `.ptz-state/skills/<id>/SKILL.md` | 带 frontmatter（name/description），供 Agent 提示词装载 |
| MCP | `.ptz-state/mcp.json` | command / args / env / enabled |

「+ 导入」填本地目录绝对路径即可。MCP 有单独的新增表单。

### 提示词


**总前置提示词**：一个 textarea，注入到**所有环节**的 system prompt 最前面。
适合放全局约定，例如「本项目是 RPG Maker MV 游戏，`\N[1]` 这类控制符必须原样保留」。

**分环节提示词**：翻译 / 校对 / 术语抽取 / 规范生成 / Agent 意图解析 / 提取脚本生成，各自独立。

- 留空 = 用内置默认（tab 上不显示圆点）
- 填了 = 已自定义（tab 上显示 ●，编辑区有「已自定义」标签）
- 「填入默认值」把当前内置默认复制进编辑区再改
- 「恢复默认」清空该环节；「全部恢复默认」清空 6 个环节 + 总前置

拼接顺序：`总前置 + 空行 + 环节提示词`。改了立即生效，下一个任务就用新提示词。

## 四、Agent 怎么理解你的话

两条路径，**规则优先**（快、不花钱），规则没命中才问模型。

| 你说 | 它做 |
| --- | --- |
| 翻译 / 汉化 / 翻一下 | `translate` |
| 校对 / 检查 / 复核 | `review` |
| 整理术语 / 名词表 | `glossary` |
| 出规范 / 风格指南 | `styleguide` |
| 分派任务 / 派活 | `plan` |
| 状态 / 进度 | `status` |
| 全流程 / 一键 / 全部 | `run`（五个串起来） |
| 翻 100 条 | `translate(limit=100)` |
| 只预览 / 别写 / 先看看 | 附加 `dry_run=true` |
| 校对，包括有疑问的 | `review(include_disputed=true)` |

多个动作会按流水线顺序排（分派 → 翻译 → 校对 → 术语 → 规范），串行执行。

听不懂时它会直说「没听懂」，并列出可用的说法 —— 不会瞎猜着执行。


---

## 五、凭据与安全

### 必须配置 API 才能执行任务


没有 API Key 时：

- 概览页顶部出现黄色提示条，Agent 输入框与「执行」按钮**禁用**
- 任何走模型的工具（translate / review / glossary / styleguide / run）任务会**直接失败**，
  错误信息指向「模型」页
- 不做「无 key 也能跑」的降级——那种产出没有意义，还会让人误以为翻译在正常工作

### API 只在本地


界面上明确标注：

> 🔒 API Key 仅保存在本机：写入 `.ptz-state/settings.json`（已收紧文件权限），
> 不上传到任何服务器，也不会出现在日志里（落盘前脱敏）。除了调用你填写的那个模型接口，不外发。
> 界面上只显示尾号，改动时需重新输入完整 Key。

具体做了五件事：

| 机制 | 实现 |
| --- | --- |
| **接口不回显** | `GET /api/settings` 只返回 `********abcd`，前端输入框永远空白，回传脱敏值不会覆盖真值 |
| **落盘前脱敏** | 日志写入前替换已知密钥 + `sk-*` / `Bearer *` / `api_key=*` / 长十六进制串 / URL userinfo |
| **文件权限收紧** | POSIX `chmod 600`；Windows `icacls /inheritance:r /grant:r <user>:F` |
| **只允许本机访问** | 校验 `Host` 与 `Origin`，非本机直接 403（挡 DNS rebinding 与跨站请求打本地 API） |
| **安全响应头** | `X-Frame-Options: DENY`（防点击劫持）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store` |

### 为什么校验 Host / Origin

WebUI 没有登录态——任何能在你机器上打开浏览器的人都能操作它。
更麻烦的是**恶意网页**：你访问一个坏站点，它的 JS 可以 `fetch('http://127.0.0.1:8787/api/...')`，
浏览器会带上请求（虽然读不到响应，但 `POST` 类副作用已经发生）。

所以校验放在后端而不是前端藏按钮：

```python
host = request.headers.get("host", "").split(":")[0].lower()
if host not in hosts:                      # 只允许 127.0.0.1 / localhost / ::1
    return JSONResponse({"error": "拒绝非本机访问"}, 403)
origin = request.headers.get("origin")
if origin and origin.split("//")[-1].split(":")[0].lower() not in hosts:
    return JSONResponse({"error": "拒绝跨站请求"}, 403)
```

`ptz serve --host 0.0.0.0` 会打印警告：API Key 仍只在本机，但**界面上的操作（含消耗额度）不设登录**。

## 六、两个安全开关

### 演练模式（默认开）

右上角复选框。开着的时候：

- 所有写操作只算不写，日志里明确标「演练模式：仅预览，未写入平台」
- 你可以放心点「全流程」看它打算干什么
- 关掉后才真写平台，会弹提示确认

### 单次运行上限

「设置 → 单次运行条数上限」（默认 200）。防止手滑点一下把几千条全翻了烧钱。

---

## 七、为什么这么设计

每个技术选择都是按**本项目的规模**做的取舍，不是抄某种技术栈：

| 维度 | 本项目做法 | 为什么 |
| --- | --- | --- |
| 前端 | **单文件原生 JS，零构建** | 用户环境没 node；`pip install` 后一条命令就要能起 |
| 实时通信 | **SSE** | 日志是单向推送，断线自动重连、代码更短 |
| 插件系统 | **工具注册表**（`TOOL_SPECS` + `build_registry`） | 动作数量固定，不需要动态加载；前端与 Agent 共用同一份能力定义 |
| 并发模型 | **单 worker 线程串行** | 平台限流按用户算，翻译和校对并发会互相抢额度、出现「刚校对完又被重译」的竞态 |
| 配置 | **`settings.json` + fingerprint 失效重建** | 改完立刻生效，不需要重启 |

刻意保留的四条：

- 一个入口命令起全套（`ptz serve`）
- 配置在界面里改，不碰文件
- 每个能力是一个可点可编排的「工具」
- 信息密度优先：一屏能看多少有用信息，而不是视觉上多空

---

## 八、API（给脚本/上层 Agent 用）

全部 JSON，`GET/POST/PUT`。文档在 `http://127.0.0.1:8787/api/docs`。

```
GET  /api/health              服务状态、是否已配置、是否 mock
GET  /api/tools               工具清单（name/label/description/params/writes/endpoint）
GET  /api/settings            配置（凭据脱敏）
PUT  /api/settings            改配置（只传要改的字段）
POST /api/settings/test       测试连接，返回项目信息 + 警告
GET  /api/project             项目概览（进度、任务、术语、TM）
GET  /api/files               文件清单
GET  /api/tasks               平台任务列表
GET  /api/jobs?limit=50       本地任务列表
POST /api/jobs                {tool, args} 提交任务
GET  /api/jobs/{id}           任务详情（含日志）
POST /api/jobs/{id}/cancel    取消
GET  /api/events              SSE 事件流（log / job / settings）
POST /api/agent               {text, execute} 自然语言 → 工具序列（可顺带执行）
                              → {steps, reply, job_ids, needs_confirm}
                              needs_confirm：危险动作（会执行本机代码）不自动执行，列在这里等确认

# 模型（源 → 模型 → 路由）
GET  /api/providers           源 + 模型 + 路由 + 每个任务当前生效项
POST /api/providers           {id, base_url, api_key, ...} 新增/更新源
DELETE /api/providers/{pid}   删源（自动清理路由引用）
POST /api/providers/{pid}/discover          拉 GET {base_url}/models
POST /api/providers/{pid}/models            加模型 {id, capabilities, temperature, ...}
PUT  /api/providers/{pid}/models/{mid}      改模型参数
DELETE /api/providers/{pid}/models/{mid}    删模型
POST /api/providers/{pid}/models/{mid}/test 单模型连通性（返回 ok/latency/error）
POST /api/providers/{pid}/models/test-all   批量（并发 3，逐项返回）
GET/PUT /api/routing                         任务 → provider::model

# 提示词
GET  /api/prompts             当前配置 + 5 个环节的内置默认值
PUT  /api/prompts             {prefix?, translate?, ...} 保存
POST /api/prompts/reset       {task} 单环节；{} 全部

# 提取
GET  /api/extractors          提取器列表（带 source: builtin/imported/agent）
POST /api/extractors          {path} 导入本地提取器目录
DELETE /api/extractors/{eid}  删除（内置不可删）
POST /api/extractors/{eid}/run            {game_dir, options, accept_risk: true} → {job}
POST /api/extractors/generate             {game_dir, hint?, accept_risk: true} → {job}
POST /api/extractors/{eid}/improve        {game_dir, feedback?, accept_risk: true} → {job}
POST /api/extract/analyze     {game_dir} 目录画像（引擎猜测/扩展名分布/样例）
GET  /api/extractions?limit=50            提取历史
GET  /api/extractions/{jid}               详情（meta + 词条分页 + 日志）
POST /api/extractions/{jid}/validate      校验原始数据 + 归并统计
POST /api/extractions/{jid}/export        导出平台 items
POST /api/extractions/{jid}/import        {file_id, filename, force?, dry_run?} 导入/预览
POST /api/extractions/{jid}/writeback     {min_stage, mode, out_dir, in_place, accept_risk, dry_run, platform_file?}
                                          → {ok, job, job_id}；原地改缺 accept_risk / mode 非法 → 400
GET  /api/extractions/{jid}/writeback/last 最近一次回写报告 {saved_at, report}

# 观测与扩展
GET  /api/usage/summary|recent|pricing    开销
GET  /api/logs[...] /api/logs/files /api/logs/download   日志
GET  /api/extensions / POST /api/extensions/install 等   插件/SKILL/MCP
```

例：

```bash
curl -X POST http://127.0.0.1:8787/api/agent \
  -H "Content-Type: application/json" \
  -d '{"text":"翻译 100 条","execute":true}'
```

```json
{"steps":[{"tool":"translate","args":{"limit":100},"why":"命中关键词"}],
 "reply":"将执行：translate","source":"rule","job_ids":["a1b2c3"]}
```

---

## 九、验证

```bat
.venv\Scripts\python.exe web_smoke.py
```

直接打 HTTP（不需要浏览器），覆盖：基础端点、配置读写与脱敏、项目数据、演练模式不写平台、
Agent 规则与参数抽取、任务队列与取消、SSE、最终状态变化，以及**回写面板**（按钮/弹窗默认值/
原地改的风险警示与倒计时/请求体/400 文案落点/轮询停止）。
用 `--mock` 起服务，不需要 Token。**断言条数随功能增长，跑一遍看实际输出，别照抄文档里的数字。**
