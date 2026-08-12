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
