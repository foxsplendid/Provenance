[English](README.md) | 中文

# Provenance

> 面向 AI 辅助科研的本地优先记忆与证据桥梁。

Provenance 把本地 AI 会话记录、Markdown 项目笔记、文献元数据与经审阅的
Agent 摘要，整理成可检索、可恢复、可追溯的科研上下文。它负责持有本地项目
记忆；Claude Code、Codex、Cursor 等 Agent 是其客户端，而不是事实源。

Provenance 是 Scriptorium 套件中可独立部署的产品。它通过版本化文件契约与
其他组件协作，不调用其他组件内部实现。

## 公开发布边界

本仓库只包含代码、文档、中立 Skill 和合成测试夹具，不包含个人对话、私人
科研语料、用户画像、真实项目状态、本机路径或私有配置。

可选的 clean-room 浏览器导出器位于 [`01-capture/`](01-capture/)。它只在用户
明确点击后运行，只请求当前 Provider 的同源接口，并在本地生成 ZIP；不包含
第三方 JavaScript、遥测、扩展存储或后台任务。相关 Provider 接口未公开且可能
变化，使用前请阅读其 [说明](01-capture/README.md) 与
[隐私边界](01-capture/PRIVACY.md)。Python 包与扩展相互独立，不会自动发现或
启动扩展。

## 核心能力

- 摄取受支持的本地编程 Agent 日志、兼容会话 ZIP、Google Takeout、Markdown
  笔记、项目组合与文献库快照。
- 提供可选、手动触发的浏览器端生产器，生成兼容的 ChatGPT 与 Claude 会话 ZIP。
- 在记录进入工作层前擦除常见凭据与 PII 形态，并可应用仅保存在本地的别名表。
- 生成确定性分类、精读输入、Markdown/Obsidian 视图和本地 SQLite FTS5 索引。
- 通过标准库实现的只读 JSON-RPC/MCP 服务向 Agent 提供研究上下文。
- 通过带锁的原子合并摄取经过验证的 `parsed-paper/1.0`、
  `reading-note/1.0`、`review/1.0` 与 `lineage-graph/1.0` 工件。
- 生成有长度上限的 Context Capsule，明确区分已批准项目状态、自动写入的
  低风险进度，以及仅供参考的文献线索。
- 将会话收尾事件写入本地队列，并通过可恢复、带锁的状态机推进。
- 项目未解析时不生成 scaffold、timeline、draft 或 `session-summary/1.0`。
- 高价值状态与结论先进入草稿，必须由用户审批后才能写回。
- 提供位于 `skills/` 的画像与交互式会话摘要 Skill；Python 包本身不调用模型。

## 产品架构

```text
本地来源
  可选浏览器导出 | Agent 日志 | 兼容 ZIP | Takeout | Markdown | 文献库快照
                               |
                               v
摄取与隐私边界
  规范化 -> 密钥擦洗 -> 可选假名化
                               |
                               v
本地科研记忆
  inbox | memory | sync-state | SQLite FTS5
            |          |
            |          +-> 待填 scaffold -> 审阅 fill -> timeline/草稿
            |                                             -> 人工审批
            v
本地消费者
  CLI | Obsidian/Markdown | 只读 MCP | Agent Skills
```

所有运行数据位于 `PROVENANCE_HOME`。该目录应与源码仓库分离；若要放入云同步
目录，必须先自行审查同步服务的隐私与保留策略。

## 环境要求

- 首发支持 Windows 10/11。
- Python 3.9 或更高版本。
- 使用 `uv` 0.11.16 复现锁定的开发环境。
- 使用两个 Agent Skill 时，需要本地已有编程 Agent。
- trigram FTS5 推荐 SQLite 3.34 或更高版本；更旧版本会明确告警并降级。

安装后的 Python 包没有第三方运行时依赖，也没有网络客户端路径。宿主 Agent
是否联网、如何保留数据，仍由宿主自身设置决定。

## Windows 安装

```powershell
git clone https://github.com/foxsplendid/Provenance.git
Set-Location Provenance
uv sync --locked

$env:PROVENANCE_HOME = "C:\ScriptoriumDemo\provenance-data"
New-Item -ItemType Directory -Force $env:PROVENANCE_HOME | Out-Null
uv run --locked prov-status
```

确认数据目录无误后再持久化 `PROVENANCE_HOME`。不要把源码仓库长期当作私人
数据根。

## 主要工作流

### 摄取与刷新

```powershell
# 受支持的本地编程 Agent 日志
uv run --locked prov-ingest-agents

# 用户自行选择的 Google Takeout ZIP
uv run --locked prov-ingest-takeout C:\ScriptoriumDemo\imports\Takeout-demo.zip

# 既有兼容 provenance__*.zip 归档
uv run --locked prov-ingest C:\ScriptoriumDemo\imports

# 重建本地整理、检索与导出
uv run --locked prov-sync
uv run --locked prov-status
```

以上均为合成路径。不要把真实归档、画像、`sync-state` 报告或本地数据根目录
列表附到公开 Issue。

### 可选浏览器导出

`01-capture/` 中的源码可在 Chrome 或 Edge 以“加载已解压的扩展程序”方式运行。
它是独立、由用户手动触发的生产器，不改变 Python 包本地运行且不联网的边界。

```powershell
Set-Location 01-capture
npm test
python -B pack.py
```

开发时直接加载 `01-capture/`，或检查其被忽略的 `dist/` 目录中的确定性归档。
Provider 内部接口并非公开契约，可能独立于 Provenance 摄取端发生变化。

### 文献与项目上下文

```powershell
uv run --locked prov-ingest-library
uv run --locked prov-ingest-research C:\ScriptoriumDemo\steward-output
uv run --locked prov-ingest-vault C:\ScriptoriumDemo\workspace
uv run --locked prov-context --project synthetic-catalyst --json
uv run --locked prov-search "research gap"
```

默认文献输入为 `%PROVENANCE_HOME%\kb\library.json`；项目组合来自工作区
`Projects/` 的 frontmatter。Obsidian 是可选示例，符合相同契约的 Markdown
工作区也可以使用。科研工件必须由用户显式指定文件或目录；命令不会发现个人
文件夹，也不会保存输入路径；输入和输出路径任一层级中的符号链接、Junction 与
Windows 重解析点都会被拒绝，读写前后还会核对文件身份，
包括 `file:` URI 在内的绝对本地路径会被脱敏，读写失败也不会回显本地路径。
Capsule 中的文献提示在研究者核对证据并批准
论断前始终只是参考线索。

### 只读 MCP

```powershell
uv run --locked prov-mcp
```

服务通过 stdio 暴露 `search_brain`、`get_profile`、`list_topics`、
`get_portfolio`、`get_current_context` 与 `get_context_capsule`，不发起网络请求，
也不写项目状态。

### 会话回写

先预览：

```powershell
uv run --locked prov-sync-pull `
  --provenance-home $env:PROVENANCE_HOME `
  --workspace C:\ScriptoriumDemo\workspace `
  --json
```

只有在明确授权后才执行一次本地写入：

```powershell
uv run --locked prov-sync-pull `
  --provenance-home $env:PROVENANCE_HOME `
  --workspace C:\ScriptoriumDemo\workspace `
  --run --json
```

待填摘要使用不透明 ID：

```powershell
uv run --locked prov-sync-pending --provenance-home $env:PROVENANCE_HOME --json
uv run --locked prov-sync-pending <summary-id> --provenance-home $env:PROVENANCE_HOME --json
uv run --locked prov-sync-fill <summary-id> --provenance-home $env:PROVENANCE_HOME
```

未解析项目会留在受保护 inflight 状态。只有用户批准项目映射后，系统才允许
产生摘要及后续写回。详细接线见 [运行手册](docs/sync-layer-wiring.md)。

## 命令概览

| 命令 | 作用 |
|---|---|
| `prov-sync` | 执行一次本地刷新流水线 |
| `prov-status` | 输出本地归档状态 |
| `prov-watch` | 监听用户指定的投递目录 |
| `prov-ingest` | 摄取兼容 `provenance__*.zip` 归档 |
| `prov-ingest-takeout` | 摄取用户选择的 Google Takeout ZIP |
| `prov-ingest-agents` | 摄取受支持的本地编程 Agent 日志 |
| `prov-ingest-library` | 摄取 `library-kb/1.x` 快照 |
| `prov-ingest-research` | 原子摄取显式选择的文献阅读工件 |
| `prov-ingest-vault` | 摄取项目 frontmatter |
| `prov-ingest-notes` | 将私人 Markdown 区域摄取进受保护状态 |
| `prov-scrub` | 对既有记录重跑当前隐私转换 |
| `prov-organize` / `prov-prep` / `prov-diff` | 生成确定性派生视图 |
| `prov-obsidian` | 导出本地 Markdown/Obsidian 视图 |
| `prov-search` | 构建或查询本地 FTS5 索引 |
| `prov-mcp` | 启动只读 stdio MCP 服务 |
| `prov-context` | 输出一个有边界、只读的项目 Context Capsule |
| `prov-sync-pull` | 预览或执行一次带锁同步 |
| `prov-sync-pending` / `prov-sync-fill` | 审阅并提交一个安全摘要 fill |
| `prov-sync-unresolved` | 只读检查未解析映射 |
| `prov-sync-approve` | 生成并应用人工审批队列 |

全部命令均支持 `--help`。

## Skills

- `skills/provenance-profile`：从脱敏精读稿生成有证据依据、去标识化的本地画像。
- `skills/session-summary`：只处理一个 allowlist scaffold，生成
  `summary-fill/1.0`，并通过公开命令提交。

两个 Skill 均禁止联网，也禁止主动读取原始 transcript 或其他会话。所有输出
保持本地，不得提交进版本库。

## 仓库结构

```text
provenance/    可安装的纯标准库 Python 包
01-capture/    可选 clean-room 浏览器导出器
skills/        中立 Agent Skills
harden/        可选 Windows 接线模板
docs/          架构、隐私与同步层契约
tests/         全合成单元与端到端夹具
tools/         公开树隐私门禁
```

`inbox/`、`drop/`、`memory/`、`profile/`、`vault/`、`sync-state/` 和
`search-index.db` 等运行数据均已忽略，必须保持私有。

## 验证

```powershell
uv run --locked python -m unittest discover -s tests -v
uv run --locked python tools/check_public_tree.py
python -B 01-capture\tests\test_extension.py
python C:\path\to\skill-creator\scripts\quick_validate.py skills\provenance-profile
python C:\path\to\skill-creator\scripts\quick_validate.py skills\session-summary
```

CI 在 Linux 与 Windows 运行 Python 和扩展测试，并检查公开树是否含有禁止发布
的私人文件、非示例身份或本机痕迹。

## 隐私与安全

脱敏只能降低误泄露风险，不等于科研数据可以公开。附件仍是原始字节；别名表
是再识别密钥；队列、草稿、日志、画像与记忆输出都可能仍然敏感。导入真实
数据前请阅读 [隐私政策](docs/PRIVACY.md)。

若怀疑存在安全问题，不要在公开 Issue 中上传数据或复现归档。先把问题缩减为
合成夹具。

## 当前状态

本地 Python 流水线、MCP 能力、事件/同步安全契约与可选浏览器导出器源码均已
实现并有测试覆盖。扩展目前采用开发者模式加载，尚未提交浏览器商店，也不对
Provider 内部接口的稳定性作保证。

## 许可证

[MIT](LICENSE)。本次公开快照的精确范围见 [NOTICE](NOTICE)。
