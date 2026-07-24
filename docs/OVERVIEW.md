# Provenance 产品概览

> 版本 0.18.0 · Scriptorium 套件中的本地科研记忆与证据桥梁。

## 1. 用户问题

科研工作者会在多个 AI 与本地工具之间反复切换。研究问题、假设、文献判断、
实验决定和下一步行动因此散落在会话、Markdown、PDF 元数据与项目目录中。
新的 Agent 往往不知道此前为何做出某个决定，也难以区分“AI 曾经建议过”与
“用户已经确认”。

Provenance 解决的不是单次问答，而是研究项目的连续性：

- 把支持的本地来源规范化到一个用户控制的数据根；
- 在进入工作层前执行凭据擦洗与可选假名化；
- 把文献、项目状态和会话线索接入同一本地检索层；
- 让 Agent 通过 CLI 或只读 MCP 获得有长度上限、带信任说明的 Context Capsule；
- 把会话收尾变成可审阅的 timeline、草稿和人工审批，而不是自动改写事实。

## 2. 产品定位

Provenance 是“项目记忆的所有者”，不是某个模型的插件。它的核心竞争力来自
四个同时成立的约束：

1. **本地优先**：运行数据由用户选择位置，Python 包无网络客户端与遥测。
2. **模型中立**：任何支持 CLI、Skill 或 MCP 的 Agent 都可以成为客户端。
3. **证据与状态分离**：原始来源、脱敏工作副本、派生摘要与审批状态分别保存。
4. **可恢复写回**：低风险 timeline 追加写入；高价值状态必须经过人工审批。

Scriptorium 套件内，Steward 负责文献处理，Lectern 负责汇报材料，Provenance
负责本地研究记忆。它们只通过 scriptorium-spec 定义的版本化文件交换。

## 3. 用户旅程

```text
模糊想法或既有积累
        |
        v
导入本地会话 / Markdown / 文献库 / 项目组合
        |
        v
脱敏、假名化、规范化、建立本地索引
        |
        v
显式摄取已验证的阅读工件，生成只读 Context Capsule
        |
        v
Agent 读取当前问题、参考证据、缺口、决定与下一步
        |
        v
一次协作结束 -> 合成 scaffold -> 人工审阅 fill
        |
        +-> timeline：可追溯追加
        +-> 高价值结论：进入 Approvals，用户批准后生效
```

项目无法解析时流程会保守停止：事件留在受保护状态，不生成 scaffold、
`session-summary/1.0` 或项目写回。

## 4. 数据与信任边界

| 层 | 示例 | 是否可公开 |
|---|---|---|
| 原始来源 | 导出 ZIP、Agent 日志、附件 | 否 |
| 脱敏工作层 | `inbox/` 中的规范化记录 | 默认仍不可公开 |
| 受保护状态 | `sync-state/`、草稿、journal、别名表 | 否 |
| 派生产物 | `profile/`、`memory/`、`vault/`、索引 | 默认仍不可公开 |
| 源码仓库 | 代码、文档、合成夹具 | 可以，经门禁检查 |

脱敏降低误泄露概率，不把私人科研内容变成公共数据。附件按原始字节处理，
尤其不能因文本记录已脱敏就假定附件安全。

Context Capsule 进一步区分三类信息：

| Capsule 内容 | 信任级别 | 使用方式 |
|---|---|---|
| 人工编写的项目字段、已批准论断 | `human_or_approved` | 可作为当前项目状态 |
| 自动追加的低风险进展 | `auto_applied_low_risk_not_approved_claims` | 仅用于恢复过程上下文 |
| 阅读工件、知识缺口与优先阅读线索 | `reference_only_not_approved_claims` | 只作为待核对参考 |

原始会话、待批草稿、被拒绝论断和无关工件不会进入 Capsule。Markdown 输出
最多 8,000 字符，JSON 输出最多 20,000 字符；绝对本地路径不会随输出暴露。

## 5. 首发能力与边界

首发目标是 Windows 上可部署、可验证的本地 Python 产品，要求用户具备配置
GitHub 开源项目和本地 Agent 的基本能力。Obsidian、Zotero 均为可选客户端或
来源；核心交换格式是 Markdown 与版本化 JSON。

已实现：

- 本地 Agent 日志、Google Takeout、兼容 ZIP、Markdown、文献库与项目组合摄取；
- 凭据/PII 擦洗、别名表假名化、分类、精读输入、Obsidian 导出；
- SQLite FTS5 本地检索与只读 MCP；
- 显式摄取 `parsed-paper/1.0`、`reading-note/1.0`、`review/1.0` 与
  `lineage-graph/1.0`，并执行稳定身份、原子幂等合并与搜索索引；
- 通过 `prov-context` 和 MCP `get_context_capsule` 生成只读、带信任分层的
  项目恢复上下文；
- 带锁、可恢复、人工门控的事件/同步层；
- 手动触发、同源请求、无第三方运行库的可选浏览器导出器；
- 两个中立 Agent Skill 与跨平台测试。

未作为当前能力发布：

- 浏览器商店签名发行，以及对 Provider 未公开内部接口的稳定性保证；
- 云端托管、后台定时模型调用或自动上传；
- 未经人工确认的结论自动晋升；
- 将所有来源内容自动视为事实；
- `experiment-run/1.0` 与 `claim-evidence/1.0` 的摄取、检索或批准流程。

研究工件必须由用户显式选择文件或目录。摄取器不发现个人目录、不保存来源
路径，并拒绝符号链接和 Windows 重解析点。包括 `file:` URI 在内的绝对
Windows、UNC 与 POSIX 路径会在落盘和建立索引前脱敏；读、解析、加锁和写入
错误不会回显来源或目标路径。

scriptorium-spec 发布实验运行与论断—证据契约只是前置条件，不代表 Provenance
已经实现相应能力；后续版本仍需单独增加验证、信任与审批语义。0.18.0 会将
这两种 schema 视为不支持，而不是默认为已有能力。

## 6. 质量证明

- 代码只依赖 Python 标准库；
- 单元与端到端测试全部使用合成夹具；
- Linux 完整测试与 Windows 同步安全测试进入 CI；
- 扩展在 Linux 与 Windows 验证 JavaScript 语法、运行时隐私边界、ZIP CRC/Unicode
  往返和打包 allowlist；
- 公开树门禁检查敏感文件名、凭据形态、非示例邮箱与已知私有夹具；
- `skills/` 通过 Skill 结构校验；
- public pull 默认只预览，写操作需要显式 `--run`；
- 合成测试覆盖研究工件的幂等与并发写入、符号链接/重解析点拒绝、`file:`
  URI 与路径脱敏、四类工件检索，以及 Capsule 的只读、长度限制和信任分层。

## 7. 下一阶段

1. 在 scriptorium-spec 契约完成后，为后续版本实现 `experiment-run/1.0`
   与 `claim-evidence/1.0` 的摄取、信任和审批语义。
2. 完成浏览器商店发行评估、真实账号兼容性验收与接口变化监测方案。
3. 建立可重复的 Windows 用户验收：部署时长、首个可检索项目、一次完整
   Context Capsule 与 writeback。
4. 为公开契约增加跨仓库兼容测试与版本迁移指南。
