<div align="center">

<img src="docs/assets/logo-full.jpg" width="760" alt="Paratranz Agent">

# Paratranz Agent

**An end-to-end agent toolkit for Chinese localization (汉化) projects on ParaTranz: plan, translate, review, glossary, and style guide, plus a bidirectional link between game files and the platform.**

[![CI](https://github.com/RandyLuaW/Paratranz-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/RandyLuaW/Paratranz-Agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/RandyLuaW/Paratranz-Agent?color=76bad9)](https://github.com/RandyLuaW/Paratranz-Agent/releases/latest)
[![License](https://img.shields.io/badge/license-source--available-lightgrey.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)](https://github.com/RandyLuaW/Paratranz-Agent/releases/latest)

[简体中文](README.md) ｜ English ｜ [WebUI Guide (in Chinese)](WebUI说明.md) ｜ [**Download for Windows**](https://github.com/RandyLuaW/Paratranz-Agent/releases/latest)

</div>

---

## ✨ What This Project Is For

The dominant cost of a localization project is not translating any individual string. It is the process work that follows: splitting thousands of strings into tasks, tracking progress, checking formatting string by string, maintaining a glossary, writing the style guide announcement, and finally writing the translations back into the game files one by one.

**Paratranz Agent** hands that workflow to an agent. It is not a bulk-upload tool, but a **platform-side workflow automation and local bidirectional file synchronization** toolkit: a single command starts the visual console, where you can enter a token, click through operations, or issue instructions in natural language.

- **One hard dependency only**: the ParaTranz OpenAPI. No third-party SDK or MCP server is required.
- **Zero front-end build**: it runs as soon as it is installed; no Node.js environment is needed.
- **No simulated model output**: if no API is configured, the tool fails with a clear error instead of degrading into a mode that appears to run but translates nothing.
- **Credentials stay on the local machine**: tokens and API keys are written only to the local `settings.json`, and logs and error messages are redacted uniformly.

---

## 🚀 Features

### Five pipelines (invocable from the `ptz` CLI or the UI)

| Pipeline | Responsibility | Implementation notes |
| --- | --- | --- |
| **Plan** `plan` | Shards work by directory, bins shards by untranslated volume, and produces task briefs with acceptance criteria | Directories are the sharding unit; deduplication keys exclude volatile counts, so repeated runs never create duplicate tasks |
| **Translate** `translate` | Exact translation-memory hits are reused directly; the remainder is submitted to the model in batches together with the glossary, TM references, and style requirements | Entries whose placeholders do not match are rejected locally and never written to the platform |
| **Review** `review` | Runs nine deterministic QA rule classes first; `error`-level findings are rejected outright, and the rest go to an LLM second pass | Verdicts are **fail-closed**: unless the model explicitly passes an entry, it does not pass |
| **Glossary** `glossary` | Extracts terms from source / finalized-translation sentence pairs, supplements them with frequency statistics, and imports them in bulk | The platform glossary is fetched before publishing; if that fetch fails, publishing is abandoned so that hand-maintained terms are never overwritten |
| **Style guide** `styleguide` | Generates Markdown from the project's actual file composition, progress, glossary, and QA rule set, then publishes it as an announcement | When an announcement with the same title exists, it is updated rather than created anew |

### Bidirectional link: game files ⇄ platform

This is what distinguishes the tool from the platform's own client: **extraction** and **writeback** share a single locator scheme.

```
 Game files ──extractor──▶ JSONL strings ──import──▶ platform ──translate/review──▶ confirmed strings
   ▲                                                                                                 │
   └─────────────────────── writeback (replace in place by key) ◀────────────────────────────────────┘
```

- **Extraction**: a pluggable extractor protocol — one directory maps to an `extractor.yaml` and an entry script, and outputs JSONL. Three extractors are built in (generic JSON, generic regex, and RPG Maker MV/MZ), and an agent can also read a game directory, generate an extractor, and trial-run it automatically.
- **Writeback**: writes confirmed translations that **meet the threshold** (default: checked) back into the game files, producing a directory that runs as a localized build.

**Key design: the key is the locator.** During extraction, the relative file path and the position within that file are encoded into the key (for example `data/Actors.json::[12].name`, `Map001.json::ev3p0[45]`, `cfg/ui.txt::L123#1`). Writeback therefore never has to re-parse a game format: it returns to the original position by key and replaces the text there.

Writeback enforces **three safety constraints** — better to skip a write than to write the wrong thing:

1. **Source-text verification**: the text currently in the file must match the source text recorded on the platform; otherwise the entry is skipped and reported as a "source text mismatch". After a game update, keys shift wholesale, and forcing a write would corrupt data.
2. **No in-place edit by default**: output goes to a separate directory. The default is a **full mirror**, in which unchanged files are copied verbatim and changed files are written as rewritten versions, so the target directory can replace the game directory directly; **changed files only** is also available.
3. **In-place edit requires explicit authorization and backs up automatically**: it needs a five-second countdown confirmation plus a mandatory backend `accept_risk` check. Backups are written to the state directory (never inside the game directory, where the next extraction would rescan them as source files) and a manifest is generated.

### Models and cost

- **Multi-provider management**: one source can host multiple models (useful for relays, aggregation gateways, and similar setups), each with independent parameters. Available models can be probed in one click, and connectivity and latency are checked per model.
- **Per-task model routing**: translate, review, glossary, style guide, agent, and extractor generation each route to a different model, so low-complexity tasks can be assigned smaller models.
- **Cost control**: every call is metered, with four budget dimensions (per call, per job, daily, monthly); when a limit is exceeded you can either warn only or hard-block.

### Other

- **Dark and light themes**: follow the system or switch manually; both palettes are chosen against contrast standards
- **Observability**: live logs over SSE, the job queue, usage and spend statistics, and data pages for strings, TM, glossary, and ledger
- **Extension slots**: plugin, SKILL, and MCP management panes (interfaces reserved)
- **Configurable prompts**: the system prompt for each of the six stages can be replaced in the UI; leaving a field empty falls back to the built-in default

---

## 🖥 Interface Preview

<table>
<tr>
<td width="50%"><img src="docs/screenshots/overview-dark.png" alt="Overview"><br/><sub><b>Overview</b>: progress, directory distribution, recent jobs, and run status on a single screen</sub></td>
<td width="50%"><img src="docs/screenshots/writeback-light.png" alt="Writeback"><br/><sub><b>Writeback</b> (light theme): select a record → set the threshold and output form → generate a report</sub></td>
</tr>
<tr>
<td><img src="docs/screenshots/extract-dark.png" alt="Extraction"><br/><sub><b>Extraction</b>: extractor management, risk warnings, and extraction records</sub></td>
<td><img src="docs/screenshots/models-dark.png" alt="Models"><br/><sub><b>Models</b>: source → model → task routing, three layers</sub></td>
</tr>
<tr>
<td colspan="2"><img src="docs/screenshots/prefs-light.png" alt="Settings"><br/><sub><b>Settings</b> (English, light theme): theme modes, interface language and about</sub></td>
</tr>
</table>

> Screenshots were taken with `ptz serve --mock` (the built-in fake-platform demo mode, which never touches a real project).

---

## 🏁 Quick Start

### Option 1: Windows portable package (no programming environment required)

Download `paratranz-agent-x.y.z-win64.zip` from
[**Releases**](https://github.com/RandyLuaW/Paratranz-Agent/releases/latest), extract it to any directory (the path may contain non-ASCII characters and spaces), then:

| File | Purpose |
| --- | --- |
| **`web.bat`** | Starts the visual console and opens `http://127.0.0.1:8787` in the browser |
| **`ptz.bat`** | Command-line entry point (equivalent to `ptz`; arguments pass through unchanged, e.g. `ptz.bat status`) |

**No Python installation and no online dependency install required**: the archive bundles the official Python runtime and all dependencies, about 20 MB in total.

- To preview the interface first: `web.bat --mock` (a built-in fake platform that exercises all five pipelines without touching a real project)
- Data lives in `.ptz-state\` inside the extracted directory (tokens, keys, jobs, and extraction records); copying that directory is a complete backup, and moving it carries everything to another machine
- Uninstalling means deleting that directory: nothing is written to the registry and no system service is installed
- The package includes `快速上手.txt` (quick start), covering first-run configuration, common problems, and uninstall instructions

### Option 2: Install from source

```bash
git clone https://github.com/RandyLuaW/Paratranz-Agent.git
cd paratranz-agent
python -m venv .venv
# Windows: .venv\Scripts\activate      Linux/macOS: source .venv/bin/activate
pip install -e .
```

Python 3.11 or newer is required. Then run:

```bash
ptz serve            # open http://127.0.0.1:8787
ptz serve --mock     # demo mode, no token required
```

On Windows you can also double-click `web.bat` in the repository.

### Configure the project and models

All configuration happens in the UI; no file editing is required:

1. **Project page**: enter the ParaTranz API token and the project ID, then click **Test connection**
2. **Models page**: configure an OpenAI-compatible API (base URL, key, model name); **Fetch model list** probes it automatically

Once this is done, you can issue instructions directly on the Overview page or act from any feature page.

### Common commands

```bash
ptz doctor                       # health check: token, permissions, platform capability flags
ptz status                       # project progress overview
ptz plan --dry-run               # dry-run the plan, no writes to the platform
ptz translate --limit 200 --yes  # translate 200 strings for real
ptz review --yes                 # review and write the results back to stage
ptz glossary --yes               # extract terms and publish them
ptz styleguide --yes             # generate the style guide and publish it as an announcement
ptz writeback <extraction-record-id> --yes  # write confirmed translations back into the game files
ptz watch                        # resident daemon: run jobs as they arrive, otherwise wait
```

Write operations default to a **dry run** and only write when `--yes` is given explicitly; every command supports `--json` for orchestration by higher-level tools.

---

## 🔐 Security Design

This tool **executes code on the user's machine** (extractors), **modifies game files** (writeback), and **incurs model usage costs** (translation). The following measures follow from that:

| Risk | Mitigation |
| --- | --- |
| An extractor amounts to arbitrary code execution on the host | A persistent warning banner, a five-second countdown confirmation, and a mandatory backend `accept_risk` check; because the check sits in the **tool layer**, none of the three entry points — the router, `POST /api/jobs`, and `/api/agent` — can bypass it |
| Prompt injection steering the agent into a dangerous action | `/api/agent` never auto-executes dangerous tools; it lists the steps awaiting confirmation, which run only after the user approves them |
| Writeback overwriting game files | Output goes to a separate directory by default; in-place edit requires explicit authorization and backs up automatically with a generated manifest |
| Credential leakage | Stored only in the local `settings.json` with tightened file permissions; the API echoes only the last few characters; logs, SSE streams, and error responses are redacted uniformly |
| Any web page calling the local WebUI | `Host` and `Origin` are validated, defending against DNS rebinding and CSRF |
| Accidentally targeting a real project | Demo and real modes are clearly distinguished in the UI with a `DEMO` marker |

---

## 📚 Documentation

| Document | Contents |
| --- | --- |
| [WebUI Guide (in Chinese)](WebUI说明.md) | How to use each page, the two safety switches, and an API overview |
| [config/agent.example.yaml](config/agent.example.yaml) | Configuration file template (everything can also be configured in the UI) |
| `ptz --help` / `<subcommand> --help` | Command-line usage (write operations default to a dry run and require `--yes`) |

---

## 🛠 Development

```bash
pip install -e ".[dev]"
python -m pytest tests -q      # unit and end-to-end tests (fully offline, using an in-memory fake platform)
python smoke.py                # full CLI smoke run
python web_smoke.py            # WebUI smoke test over real HTTP requests
python -m ruff check --select F,E9,B,S102,S301,S307,S506,S608,ASYNC src tests smoke.py web_smoke.py tools
```

CI runs Python 3.11 and 3.13 on both ubuntu and windows. The repository also ships several tools that validate against the real platform (they require your own token; use them with care):

- `tools/build_portable.py`: builds the Windows portable package (downloads the official embeddable Python, packs the zip, and generates self-check data and a sha256), completing a release in one command
- `tools/realcheck.py`: health check against the real platform, covering endpoint response shapes, QA rule distribution, sampling re-review, and stage reset
- `tools/verify_writeback_real.py`: end-to-end writeback validation against the real platform (creates its own temporary files and cleans them up afterwards)
- `tools/check_spec_coverage.py`: compares `client.py` with the official OpenAPI spec via AST to catch wrong endpoints and parameter names

---

## ⚖️ Disclaimer and Free-of-Charge Notice

### Scope of liability

This project is a **third-party tool** and is not affiliated with ParaTranz. It calls the ParaTranz API under the user's identity, so please confirm your project permissions and the platform's terms yourself.

Destructive actions (running extractor scripts, rewriting game files in place, sending write requests to a real project) all require explicit user confirmation, and **you bear the consequences**. Validate them in `--mock` demo mode or on an idle project before using a live one.

### This software is completely free

This software is **completely free of charge**. The only official distribution channel is this repository: <https://github.com/RandyLuaW/Paratranz-Agent>.

If you obtained it through any paid channel (paid download sites, resold bundles, paid installation services, "members-only" collections), please request a refund immediately and download it from the Releases page at no cost. The official repository never charges you, and there is no "activation code", "license key", or "insider build" to buy.

The same notice appears in the console on first launch (bilingual), and permanently under Settings → About. Full text: [`NOTICE.txt`](NOTICE.txt); the portable package also ships `免费声明.txt`.

---

## 📄 License

This project uses a [custom source-available license](LICENSE). It is **not** an OSI-approved open-source license; the English text in `LICENSE` is authoritative.

In short: free to use, modify and redistribute at no charge; redistribution must credit the original author (RandyLuaW) and include the official repository link <https://github.com/RandyLuaW/Paratranz-Agent>; providing this software (or a modified version) to third parties as a network service for a fee — in any form — is not permitted. All rights not expressly granted are reserved by the author.

Third-party dependencies keep their own licenses; see [`THIRD-PARTY-LICENSES.txt`](THIRD-PARTY-LICENSES.txt). For other arrangements (closed-source integration, for example) please open an issue.
