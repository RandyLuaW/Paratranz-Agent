<div align="center">

<img src="docs/assets/logo-full.jpg" width="760" alt="Paratranz Agent">

# Paratranz Agent

**让 LLM Agent 接管 ParaTranz 汉化项目的全流程 —— 分派、翻译、校对、术语、规范，以及「游戏文件 ↔ 平台」的双向链路。**

[![CI](https://github.com/RandyLuaW/Paratranz-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/RandyLuaW/Paratranz-Agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/RandyLuaW/Paratranz-Agent?color=76bad9)](https://github.com/RandyLuaW/Paratranz-Agent/releases/latest)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)](#)

简体中文 ｜ [WebUI 说明](WebUI说明.md) ｜ [**下载 Windows 免安装包**](https://github.com/RandyLuaW/Paratranz-Agent/releases/latest)

</div>

---

## ✨ 这是什么

`ParaTranz` 上做汉化，真正耗时的从来不是"翻一条"，而是那些重复劳动：把几千条词条切成任务、盯着进度、逐条校对格式、维护术语表、写规范公告、最后再把译文一条条塞回游戏文件。

**Paratranz Agent** 把这些交给 Agent 做。它不是一个"批量上传工具"，而是一套**平台侧全流程自动化 + 本地文件双向同步**工具：一条命令起一个可视化控制台，你在界面里填 Token、点按钮、或者直接用中文下指令。

- **唯一的硬依赖**是 ParaTranz OpenAPI（不依赖任何第三方 SDK 或 MCP）
- **零构建前端**：`pip install` 之后一条命令就能用，不需要 Node
- **不模拟模型**：没配 API 就直接报错，不给你"看起来在跑其实没翻"的假象
- **凭据只在本机**：Token / API Key 只写本地 `settings.json`，日志与错误信息全部脱敏

---

## 🚀 功能

### 五条流水线（都可用 `ptz` 命令行或界面触发）

| 流水线 | 做什么 | 关键点 |
| --- | --- | --- |
| **分派** `plan` | 按目录分片、按未翻译量装箱，生成带验收标准的任务说明 | 目录即分片单位；去重键不含可变计数，重跑不会建重复任务 |
| **翻译** `translate` | 翻译记忆精确命中直出，其余分批送模型（带术语表 + TM 参考 + 风格） | 占位符不一致的**在本地就拦掉**，不写回平台 |
| **校对** `review` | 9 类确定性 QA 规则先跑，`error` 直接打回；其余进 LLM 二审 | 判定 **fail-closed**：模型没明确说"通过"就不算通过 |
| **术语** `glossary` | 模型从"原文 / 已定稿译文"句对抽术语，词频统计补位，批量导入 | 发布前先拉平台术语表，拉不到就放弃发布（不覆盖人工术语） |
| **规范** `styleguide` | 由项目实际文件构成、进度、术语表、QA 规则集生成 markdown，发布为公告 | 同标题公告存在时更新而非新建 |

### 双向链路：游戏文件 ⇄ 平台

这是它区别于"平台客户端"的地方 —— **提取**和**回写**共用同一套定位符：

```
游戏文件 ──提取器──▶ JSONL 词条 ──导入──▶ 平台 ──翻译/校对──▶ 确认译文
   ▲                                                              │
   └──────────────── 回写（按 key 回原位替换）◀──────────────────┘
```

- **提取（Extraction）**：可插拔提取器协议（一个目录 = `extractor.yaml` + 入口脚本，JSONL 输出）。内置通用 JSON / 通用正则 / RPG Maker MV·MZ 三个，也能让 **Agent 现场读你的游戏目录、写一个提取器**并自动试跑。
- **回写（Writeback）**：把平台上**达到门槛**（默认"已检查"）的确认译写回游戏文件，产出一份**完整可跑**的汉化版目录。

**关键设计——key 就是定位符。** 提取时 key 编码了「文件相对路径 + 文件内位置」（`data/Actors.json::[12].name`、`Map001.json::ev3p0[45]`、`cfg/ui.txt::L123#1`），所以回写不需要重新理解游戏格式，按 key 回到原位替换即可。

回写有**三条安全线**（宁可少写，不可错写）：

1. **原文校验** —— 文件里当前这段必须等于平台记录的原文，否则跳过并报「原文不一致」。游戏本体更新过之后 key 会整体错位，硬写就是毁数据。
2. **默认不原地改** —— 产出到独立目录。默认 **整包镜像**：没改动的文件原样复制、改动文件直接写改写版，目标目录可直接替换游戏目录；也可以只输出被改动的文件。
3. **原地改要显式授权 + 自动备份** —— 5 秒倒计时勾确认 + 后端 `accept_risk` 硬拦，备份落在状态目录（不放进游戏目录，否则下次提取会把备份当素材再扫一遍）并写 manifest。

### 模型与成本

- **多 provider 管理**：一个源可挂多个模型（中转站 / 聚合网关的常态），每个模型单独配参数，支持一键探测可用模型、逐模型连通性与延迟检查。
- **按任务分配模型**：翻译 / 校对 / 术语 / 规范 / Agent / 提取器生成各自路由到不同模型，便宜任务用小模型。
- **成本控制**：逐次调用记账 + 四维预算（单次 / 单任务 / 每日 / 每月），超限可只警告或直接拦。

### 其它

- **深色 / 浅色主题**，跟随系统或手动切换，两套都按对比度选过
- **观测**：实时日志（SSE）、任务队列、用量与花费统计、数据页（词条 / TM / 术语 / 账本）
- **扩展位**：插件 / SKILL / MCP 三栏管理（预留）
- **提示词可改**：六个环节的 system prompt 都能在界面里替换，留空即用内置默认

---

## 🖥 界面

<table>
<tr>
<td width="50%"><img src="docs/screenshots/overview-dark.png" alt="概览"><br/><sub><b>概览</b>：一屏看完进度、目录分布、最近任务、状态</sub></td>
<td width="50%"><img src="docs/screenshots/writeback-light.png" alt="回写"><br/><sub><b>回写</b>（浅色主题）：选记录 → 定门槛与形态 → 出报告</sub></td>
</tr>
<tr>
<td><img src="docs/screenshots/extract-dark.png" alt="提取"><br/><sub><b>提取</b>：提取器管理 + 风险警示 + 提取记录</sub></td>
<td><img src="docs/screenshots/models-dark.png" alt="模型"><br/><sub><b>模型</b>：源 → 模型 → 任务路由，三层结构</sub></td>
</tr>
</table>

> 截图来自 `ptz serve --mock`（内置假平台演示模式，不碰真实项目）。

---

## 🏁 快速开始

### 方式一：Windows 免安装包（不写代码就用这个）

到 [**Releases**](https://github.com/RandyLuaW/Paratranz-Agent/releases/latest) 下载
`paratranz-agent-x.y.z-win64.zip`，解压到任意目录（路径含中文、空格都行），然后：

| 双击 | 作用 |
| --- | --- |
| **`web.bat`** | 打开可视化控制台，浏览器自动打开 `http://127.0.0.1:8787` |
| **`ptz.bat`** | 命令行（等价于 `ptz`，参数照传，如 `ptz.bat status`） |

**不需要装 Python，也不需要联网装依赖**——包里自带官方 Python 运行时和全部依赖，约 21 MB。

- 想先看看界面：`web.bat --mock`（内置假平台，完整演示五条流水线，不碰真实项目）
- 数据都在解压目录的 `.ptz-state\` 里（Token、Key、任务与提取记录）；备份就复制这个文件夹，换机器带走即可
- 卸载 = 删掉文件夹，不写注册表、不装服务
- 包里附带 `快速上手.txt`，写了首次配置、常见问题与卸载

### 方式二：从源码安装

```bash
git clone https://github.com/RandyLuaW/Paratranz-Agent.git
cd paratranz-agent
python -m venv .venv
# Windows: .venv\Scripts\activate      Linux/macOS: source .venv/bin/activate
pip install -e .
```

需要 Python ≥ 3.11。然后：

```bash
ptz serve            # 打开 http://127.0.0.1:8787
ptz serve --mock     # 没有 Token 也能看的演示模式
```

Windows 上也可以直接双击仓库里的 `web.bat`。

### 3. 填两样东西

在界面里配置，不需要改文件：

1. **「项目」页** —— ParaTranz API Token + 项目编号（点「测试连接」）
2. **「模型」页** —— 一个 OpenAI 兼容的 API（地址 / Key / 模型名，可点「获取模型列表」自动探测）

然后就能在「概览」页直接下指令，或到各页面点按钮。

### 常用命令

```bash
ptz doctor                       # 体检：Token、权限、平台能力开关
ptz status                       # 项目进度概览
ptz plan --dry-run               # 预演分派，不落地
ptz translate --limit 200 --yes  # 真翻 200 条
ptz review --yes                 # 校对并写回 stage
ptz glossary --yes               # 抽术语并发布
ptz styleguide --yes             # 生成规范并发布为公告
ptz writeback <提取记录id> --yes  # 把确认译文写回游戏文件
ptz watch                        # 常驻守护：有活就干，没活就等
```

写操作默认 **dry-run**，必须显式 `--yes` 才落地；所有命令都支持 `--json` 供上层编排。

---

## 🔐 安全设计

这套工具会**在你的机器上执行代码**（提取器）、**改动你的游戏文件**（回写）、**替你花模型的钱**（翻译）。所以：

| 风险 | 处置 |
| --- | --- |
| 提取器 = 本机任意代码执行 | 常驻警示条 + 5 秒倒计时确认 + 后端 `accept_risk` 硬拦。闸门在**工具层**，所以路由、`POST /api/jobs`、`/api/agent` 三个入口都绕不过去 |
| Agent 被提示注入触发危险动作 | `/api/agent` 不自动执行危险工具，只把待办列出来让你手动确认 |
| 回写覆盖游戏文件 | 默认产出到独立目录；原地改需显式授权 + 自动备份 + manifest |
| 凭据泄露 | 只存本地 `settings.json`（权限收紧）；接口只回显尾号；日志、SSE、错误响应统一脱敏 |
| 任意网页调用本机 WebUI | 校验 `Host` / `Origin`，挡 DNS rebinding 与 CSRF |
| 误连真实项目 | 演示模式与真实模式在界面上明确区分（`DEMO` 标记） |

---

## ⚠️ 几个实测出来的平台约束

这些是踩过坑之后写进代码的，不是抄文档：

- **列表接口返回分页包** `{results,...}`，只有 `/files`、`/files/{id}/translation`、`/members` 是裸数组
- **`POST /strings` 必须带 `translation`**（哪怕空串），否则服务端 500 —— spec 里没标必填
- **`POST /issues` 必须带 `title`**，`stringId` 被静默忽略；平台**没有词条级评论接口**，打回理由只能发成项目级讨论帖
- **`PUT /strings` 的 `edit` 分级权限**：翻译只能改未审核词条 → `pass_stage=5` 之后 `translate` 改不动那些词条
- **`PUT /terms` 是 upsert 不是全量同步**（实测：只提交一半，其余一条没丢）
- **列表字段可能是 `null`**（如 `Term.variants`），模型必须容忍
- 全局限流 **480 次/分钟**按用户计；`suggestions` 接口在 ES 上检索，必须串行

---

## 📚 文档

| 文档 | 内容 |
| --- | --- |
| [WebUI 说明](WebUI说明.md) | 每个页面怎么用、两个安全开关、API 一览 |
| [config/agent.example.yaml](config/agent.example.yaml) | 配置文件模板（也可以全在界面里配） |
| `ptz --help` / 各子命令 `--help` | 命令行用法（写操作默认 dry-run，需 `--yes`） |

---

## 🛠 开发

```bash
pip install -e ".[dev]"
python -m pytest tests -q      # 单元 + 端到端（全部离线，用内存假平台）
python smoke.py                # CLI 全流程冒烟
python web_smoke.py            # WebUI 冒烟（真 HTTP）
python -m ruff check --select F,E9,B,S102,S301,S307,S506,S608,ASYNC src tests smoke.py web_smoke.py tools
```

CI 在 ubuntu + windows 上各跑 3.11 / 3.13 两版。仓库里还有几个真机验证工具（需要你自己的 Token，慎用）：

- `tools/build_portable.py` —— 构建 Windows 免安装包（下载官方 embeddable Python + 打 zip + 出自检与 sha256），发版一条命令
- `tools/realcheck.py` —— 真实平台体检：端点返回形态 / QA 规则分布 / 抽样复核 / stage 复位
- `tools/verify_writeback_real.py` —— 回写全链路真机验证（自建临时文件，跑完自清）
- `tools/check_spec_coverage.py` —— 用 AST 比对 `client.py` 与官方 OpenAPI spec，查打错端点/参数名

---

## ⚖️ 免责声明

本项目是**第三方工具**，与 ParaTranz 官方无关。它会以你的身份调用 ParaTranz API，请自行确认你的项目权限与平台条款。

危险动作（执行提取器脚本、原地改写游戏文件、对真实项目发写请求）都需要你显式确认，**后果由使用者自行承担**。建议先在 `--mock` 演示模式或闲置项目上跑通再用于正式项目。

---

## 📄 License

[GNU Affero General Public License v3.0](LICENSE)

简单说：你可以自由使用、修改、分发；但如果你把它（或修改版）作为网络服务提供给别人用，**必须公开源代码**。如果你需要更宽松的授权（例如闭源集成），欢迎开 issue 讨论。
