# Provenance

[![CI](https://github.com/foxsplendid/Provenance/actions/workflows/ci.yml/badge.svg)](https://github.com/foxsplendid/Provenance/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/foxsplendid/Provenance)](https://github.com/foxsplendid/Provenance/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Provenance 是一个本地优先的项目记忆引擎，用于长期 AI 协作。它把本地工作历史、AI 对话、笔记和项目记录整理成可搜索、可恢复、可审查的上下文，并通过命令行和 MCP 提供给后续 Agent 使用。

![Provenance 记忆闭环](docs/assets/provenance-loop.svg)

## 它解决什么问题

AI 协作最大的问题之一是连续性。下一次会话往往不知道上一次哪些判断已经被接受、哪些来源支撑了判断、哪些只是草稿、哪些应该忽略。Provenance 的目标是把这种连续性显式保存下来，同时默认不把用户的私有项目记忆上传到托管服务。

在 Scriptorium 套件中，Provenance 是记忆和溯源层。它也可以脱离套件独立使用，用于长期代码项目、科研项目、写作项目或运维工作。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 摄取 | 读取用户明确选择的本地 Agent 日志、导出对话、笔记、归档和项目记录。 |
| 清洗与整理 | 区分原始输入和派生记忆，支持脱敏，并重建本地索引。 |
| 搜索 | 在整理后的项目记忆中进行本地搜索。 |
| 上下文胶囊 | 为未来 AI 会话生成有边界的恢复上下文。 |
| MCP | 向兼容 Agent 宿主暴露只读项目上下文。 |
| 同步回写 | 通过审批入口支持可审查的会话回写。 |
| Capture | 提供可选浏览器扩展，用于导出 ChatGPT/Claude 对话历史并本地摄取。 |

## 快速开始

```powershell
git clone https://github.com/foxsplendid/Provenance.git
cd Provenance
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\prov-status.exe --help
```

从用户选择的本地来源创建或刷新记忆库：

```powershell
.\.venv\Scripts\prov-ingest-agents.exe --help
.\.venv\Scripts\prov-ingest-takeout.exe --help
.\.venv\Scripts\prov-search.exe --help
```

生成项目上下文胶囊：

```powershell
.\.venv\Scripts\prov-context.exe --help
```

## Capture 浏览器扩展

Capture 是从本仓库发布的可选浏览器导出工具。当 AI 会话历史只能从浏览器页面取得时，它用于帮助用户把选定对话导出成本地文件。

它以 release asset 形式发布，也可以通过 Scriptorium 单独安装：

```powershell
scriptorium install capture --target D:\Tools\scriptorium-capture --run
```

这个扩展不代表 Provenance 是浏览器产品；它只是补齐“从浏览器拿回本地对话历史”的入口。

## 在套件中的位置

```text
Scriptorium 工作区
        │
        ├── 来源文件与项目配置
        ├── Steward 文献 handoff
        ├── Scriptorium Spec 数据契约
        └── Provenance 记忆库
                ├── 原始捕获材料
                ├── 可搜索整理记录
                ├── 可审查会话回写
                └── 提供给未来 Agent 的上下文胶囊
```

Provenance 在需要跨组件协同时读写公开文件契约。它不应该被理解成其他组件私下调用的隐藏数据库。

## 命令族

| 类型 | 命令 |
| --- | --- |
| 摄取 | `prov-ingest`、`prov-ingest-agents`、`prov-ingest-takeout`、`prov-ingest-library`、`prov-ingest-vault`、`prov-ingest-notes` |
| 整理 | `prov-organize`、`prov-prep`、`prov-diff`、`prov-scrub` |
| 搜索与上下文 | `prov-search`、`prov-context`、`prov-status`、`prov-mcp` |
| 同步层 | `prov-sync-*` 系列命令，用于 enqueue、fill、pending review、approval、pull、unresolved 和 worker 流程 |
| 可选导出 | `prov-obsidian` |

## 隐私与安全模型

Provenance 围绕三个边界设计：

- 原始来源不等于项目记忆。
- AI 生成摘要不会自动成为已接受结论。
- 会话回写需要明确的审批入口。

项目默认本地优先，但它仍然会读取你指定的敏感本地材料。不要把私有来源提交到公开仓库；分享前先审查导出内容；展示时使用合成样例。

## 文档

- [Overview](docs/OVERVIEW.md)
- [Privacy](docs/PRIVACY.md)
- [Specification notes](docs/SPEC.md)
- [Sync-layer wiring](docs/sync-layer-wiring.md)
- [Capture README](01-capture/README.md)

## 开发

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## 许可证

MIT。见 [LICENSE](LICENSE)。
