# SagaSmith CoC MCP

[中文](README.md) · [English](README-en.md) · [官网](https://sagasmithai.github.io) · [平台总览](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md) · [托管服务](https://github.com/SagaSmithAI/SagaSmith-service) · [内容目录](https://github.com/SagaSmithAI/SagaSmith-dnd-content-library)

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

Snapshot 在公共协议中仍是可独立恢复的完整状态文档；底层 schema v8 仅把每个文档独立压缩为 `zlib-1` 记录，并校验压缩字节、文档 checksum 与节点身份。`snapshot_query/change`、branch checkout、undo/redo 和重启恢复都不依赖祖先链回放。

服务启动时会执行 Core Alembic 迁移，并要求数据库符合当前 Snapshot schema v8。部署前必须在服务停止且 SQLite WAL 已收敛后备份 `data/ttrpgbase.db`；外部数据库使用其原生一致性备份。当前格式不可 downgrade；回滚必须将数据库、Core、CoC 和 MCP 恢复为一套匹配版本。

Play 与 Combat 阶段提供两项来源明确的角色状态结算：

- `coc_sanity_check` 原子完成 SAN 检定、损失骰、必要的 INT 检定、临时/不定期/永久疯狂、狂乱发作与持续时间，并在同一 revision group 中提交战役随机流和调查员 sheet。
- `coc_hp_change` 原子完成伤害或治疗；单次重伤会使用权威随机流执行必要的 CON 检定，并持久化 major wound、unconscious、dying、dead 与治疗状态。无随机抽取的纯 HP 变更不会伪造战役 revision。

两项工具都要求角色控制权限、campaign/character revision 和幂等键；精确重试返回原响应，不能重复抽取或重复结算。

权威战斗使用任务型原生工具，而不是让调用方直接改写 `campaign.state`：

```text
combat_start -> combat_query
             -> combat_action(move|join|end_turn)
             -> combat_attack(open -> resolve|abort)
             -> combat_end
```

`combat_start` 校验参与者的角色 revision，并以 DEX、已准备枪械的 DEX+50 和稳定同值顺序进入 Combat。攻击先持久化待响应选择；目标控制者再选择闪避、反击、俯身找掩护或不响应。`resolve` 从战役随机流结算攻击、防御、极难/贯穿伤害、弹药、CON、HP 与伤势，并把战役和受影响角色写入同一 revision group。Grid 模式由引擎保存坐标和校验移动/近战距离；Agent 模式不生成坐标，只接受 Agent 明确给出的空间事实。`combat_end` 返回 Play，并列出仍需濒死恢复处理的角色。

真实 stdio 宿主回归已覆盖 Lobby → Play → Combat → Play：每次阶段变化后 Host 刷新原生列表，旧阶段工具立即消失，新阶段工具可直接加载和调用。

追逐在 Play 内由 `chase_start/query/action/end` 管理，并与 Combat 严格互斥。开始追逐时，MCP 从角色 sheet 读取明确指定的 CON、Drive Auto 或 Pack 技能，使用战役随机流结算速度检定，再按最慢有效 MOV 计算每轮行动点。`chase_action` 权威维护 DEX 顺序、行动点消耗、路线位置、障碍检定和回合重置；障碍成功/失败对应的位置变化与来源必须由 Pack 或 Agent 明确提供，MCP 不猜测叙事地形。玩家只能操作被授权角色，开始/结束追逐只对 Keeper 开放，所有随机和状态变更均具 revision 与精确幂等收据。

调查连续性使用彼此分离的三类账本，不能把叙述自动当成所有角色都知道的事实：

- `campaign_event` 写入分支内时间线，必须显式给出 `dm`、`party`、`public` 或 `actor` 受众，并可标记 speaker/listener/witness/target 参与者。
- `continuity_context` 返回受统一字符预算约束的分支上下文。非 Keeper 调用始终强制使用玩家投影，只能读取自己获授权角色的私有知识。
- `memory_change(action="commit")` 将一个事件、客观事实修订、逐角色知识修订及可选快照原子结算；派生事实与知识默认引用同一来源事件。精确重试返回原响应，任何子项失败都会整体回滚。

客观 `memory_query` 和全部连续性写入只对 Keeper 开放，玩家不能借客观事实账本绕过线索、秘密、错误信念或分队边界。Combat 期间保留安全的连续性读取，但关闭时间线与记忆写入，回到 Play 后再恢复；真实 stdio 回归验证这些原生工具随阶段 schema 正确出现和消失。

来源明确的调查检定使用 `investigation_check(open|spend_luck|push|settle|abort)` 与 `investigation_query`。MCP 从角色 sheet 读取精确命名的技能、特征或 Luck，使用战役随机流掷骰，并持久化尚未完成的人类选择。Spending Luck 必须由战役设置显式启用，精确花费与角色 revision 原子结算；孤注一掷必须给出新的行动方式和 Keeper 预告的失败代价，第二次掷骰后不能再花 Luck。待决选择可跨重启恢复，并会阻止进入 Combat、Chase 或返回 Lobby，直到结算或由 Keeper 中止；成功技能只标记一次，留待会后成长。

检定不会猜测线索含义或受众。`settle` 返回机械收据，再由 Agent 通过 `memory_change(action="commit")` 落账来源特定的叙述、客观事实、逐角色知识和孤注一掷失败代价。显然或不可缺少的线索完全绕过检定，直接使用连续性结算，不能因连续坏骰阻断模组。

组合检定复用同一套可恢复流程，但一次 d100 会同时对比两个到八个从角色 sheet 读取的技能或特征。Keeper 必须明确选择 `requirement="any"` 或 `"all"`；花费 Luck 只能精确购买这个聚合要求，每个成功的技能分量分别获得成长标记。CoC 不会虚构 D&D 式“多数成功”团体规则。真正的团体 Luck 由 `group_luck_query/check` 读取现场所有参与者的当前 Luck，只允许最低 Luck 的调查员代表；最低值并列时必须由 Keeper 显式选择。

会后成长只在 Lobby 开放。`development_query` 列出已勾选技能，`development_settle` 在一次战役随机流事务内完成全部成长骰、技能更新、首次 mastery SAN 奖励、勾选清空与审计回执；Cthulhu Mythos 会被明确标为不适用普通成长并清除错误勾选。写入边界同时校验角色控制权、战役/角色 revision、分支和幂等重放。

## Module Pack 创作流程

CoC 模组使用统一的 `sagasmith.content-package` schema v2：

```text
module_draft(start)
  -> module_draft(edit, operation="advance")  # 仅在首遍中断时恢复
  -> module_draft(evidence)
  -> module_draft(edit, operation="statblock|content|asset|actor")
  -> module_draft(edit, operation="package")
  -> module_draft(finalize)
  -> content_pack(import)
  -> content_pack(activate)
  -> content_pack(deactivate|remove)
```

`start` 接受导入白名单中的 PDF、Markdown、文本 `source_path`，或生成内容的 `name` 加 `content`。机械导入只产生未激活草稿；若进程在已提交的中间步骤后中断，`advance` 会从该步骤继续。`evidence` 提供有界文本块、受管 PDF 页面渲染收据、资产和内容审阅；`edit` 支持 checksum 绑定的 PDF 文本修订、CoC 内容审阅、当前 CoC statblock schema 校验、白名单资产、演员绑定和 Pack 决策。statblock 可保留来源中真实但不完整的非战斗 NPC 数据；只有显式声明 `combat_ready` 时才强制战斗必需字段。修改来源文本会创建新的未激活机械版本，并使下游草稿决定失效。游玩配置和目录决策必须引用 `evidence` 返回的原样来源收据。终结需要 Agent 显式确认，并生成不可静默修改的 `.sagasmith-pack`；只有从该最终档重新导入的模块才能激活。

商业规则书和模组始终保留在本地。用 `SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS` 配置允许读取的来源根目录，多个路径使用系统路径分隔符。仓库不分发原书、抽取文本或原书资产。

Pack 导入使用确定性恢复协议：Pack checksum 属于候选版本身份，module、asset、
content review、actor 与 binding 的每一步都按内容身份或子幂等键收敛。进程在最终回执
前中断时，用原请求和 `idempotency_key` 重试即可继续，不能产生重复运行时对象。
激活、停用和删除各自提交精确回执；删除后的相同请求仍可重放原响应。

## 启动

```bash
pip install -e "../sagasmith-core[documents]"
pip install -e ../sagasmith-coc
pip install -e .
sagasmith-coc-mcp
```

统一本地栈使用 streamable HTTP 权威服务与粘性会话 Workbench gateway：

```powershell
$env:SAGASMITH_COC_MCP_TRANSPORT = "streamable-http"
$env:SAGASMITH_COC_MCP_HTTP_PORT = "8769"
sagasmith-coc-mcp

# 另一个终端
$env:SAGASMITH_COC_MCP_URL = "http://127.0.0.1:8769/mcp"
$env:SAGASMITH_COC_GATEWAY_PORT = "8768"
sagasmith-coc-gateway
```

浏览器不能提交 principal。Gateway 在服务端绑定身份，为每个浏览器/战役保留独立
MCP session，并在 `tools/list_changed` 后刷新真实原生工具列表。

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
