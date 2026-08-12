# SagaSmith CoC MCP

[中文](README.md) · [English](README-en.md) · [平台总览](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md)

SagaSmithAI 的 Call of Cthulhu 7e 本地权威 MCP 服务。它把 `sagasmith-core` 的战役持久化、分支记忆、角色知识、快照、模组检索和统一 Content Pack，与 `sagasmith-coc` 的 d100、理智、战斗、追逐和可重放随机流整合为一个原生 MCP 边界。

## 运行时边界

- MCP 负责权威战役状态、权限、revision、幂等性、随机流收据和随机判定的原子提交。
- 每个 MCP session 独立维护原生工具 exposure；Lobby、Play、Combat 策略还会在调用时再次校验。
- Host 必须响应 `tools/list_changed` 并刷新原生 schema；没有固定工具全集、文本模拟或 `exposure_call` fallback。
- Agent 负责解释来源和作出模组特有的语义决策；最终 Pack 保留这些决定的来源证据。

原生能力加载流程：

```text
exposure(open) -> exposure(search) -> exposure(set) -> native domain tool
```

Keeper 恢复接口由 `branch_query/change`、`snapshot_query/change` 和
`state_revision` 组成。所有写操作都要求显式 revision/分支或历史游标守卫以及
`idempotency_key`；checkout、restore、undo、redo 改变权威阶段后会触发
`tools/list_changed`。Host 刷新列表后可重新加载并直接调用该阶段的合法原生工具。

## Module Pack 创作流程

CoC 模组使用统一的 `sagasmith.content-package` schema v2：

```text
module_draft(start)
  -> module_draft(evidence)
  -> module_draft(edit, operation="package")
  -> module_draft(finalize)
  -> content_pack(import)
  -> content_pack(activate)
```

`start` 接受导入白名单中的 PDF、Markdown、文本 `source_path`，或生成内容的 `name` 加 `content`。机械导入只产生未激活草稿。`evidence` 提供有界文本块、受管 PDF 页面渲染收据、资产和内容审阅；`edit` 支持 checksum 绑定的 PDF 文本修订、CoC 内容审阅、白名单资产、演员绑定和 Pack 决策。修改来源文本会创建新的未激活机械版本，并使下游草稿决定失效。游玩配置和目录决策必须引用 `evidence` 返回的原样来源收据。终结需要 Agent 显式确认，并生成不可静默修改的 `.sagasmith-pack`；只有从该最终档重新导入的模块才能激活。

商业规则书和模组始终保留在本地。用 `SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS` 配置允许读取的来源根目录，多个路径使用系统路径分隔符。仓库不分发原书、抽取文本或原书资产。

## 启动

```bash
pip install -e "../sagasmith-core[documents]"
pip install -e ../sagasmith-coc
pip install -e .
sagasmith-coc-mcp
```

状态默认位于 `.sagasmith-coc-mcp/`。主要配置项：

- `SAGASMITH_COC_MCP_HOME`
- `SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS`
- `SAGASMITH_COC_SKILLS_DIR`
- `SAGASMITH_MODULEGEN_SKILLS_DIR`
- `SAGASMITH_COC_MCP_BOUND_PRINCIPAL_ID`

## 开发

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

原创代码采用 Apache-2.0。Call of Cthulhu 及相关商业内容的权利归各自权利人所有。
