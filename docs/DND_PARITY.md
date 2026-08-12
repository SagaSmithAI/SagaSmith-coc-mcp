# SagaSmith CoC 对标 D&D 的功能、契约与证据矩阵

本文件以 2026-08-12 的当前工作树为基线。对标指概念能力、权威边界和公共宿主行为达到同等级，不要求把 D&D 特有的职业、法术位或网格规则复制到 CoC。

状态：`完成` 表示已有公共 facade 与对应回归证据；`部分` 表示只有部分路径或证据；`缺失` 表示尚无当前协议；`阻塞` 只用于外部边界，本矩阵目前没有阻塞项。

## 基线规模

| 仓库 | 当前事实 | 结论 |
| --- | --- | --- |
| `sagasmith-dnd` | 107 个受版本控制文件、45 个 Python 测试文件 | 规则运行时参考实现 |
| `sagasmith-coc` | 29 个受版本控制文件、5 个 Python 测试文件 | 仅具备基础判定、随机流和 Module Pack 编译 |
| `SagaSmith-dnd-mcp` | 64 个 `public_tool`、80 个 Python 测试文件 | 全链路公共行为参考实现 |
| `SagaSmith-coc-mcp` | 27 个 MCP 工具、1 个 Python 测试文件、14 项测试 | 已有创作、恢复、随机流与 SAN/HP 权威结算垂直切片，尚未形成完整运行时 |
| `SagaSmith-dnd-skills` | 4,157 个受版本控制文件 | 包含完整技能、引用、模板和内容语料 |
| `SagaSmith-coc-skills` | 34 个受版本控制文件 | 只有 Keeper、战役管理和少量静态引用 |
| `sagasmith-dnd-ui` | 59 个受版本控制文件 | 含 Content Workbench、场景图谱和战斗工作区 |
| `sagasmith-coc-ui` | 28 个受版本控制文件 | 只有基本战役、调查员和场景页面 |

文件数量只用于揭示审计范围，不是完成标准。

## 运行时与 MCP

| 能力域 | D&D 当前公共证据 | CoC 当前状态 | CoC 完成证据要求 |
| --- | --- | --- | --- |
| 原生动态工具 | `exposure`、会话级列表、`tools/list_changed`、调用时二次校验 | 完成 | stdio 客户端能 open/search/set，刷新后直接调用；跨 session、阶段、角色隔离回归 |
| Lobby → Play → Combat → Play | `game_phase`、`combat_start/end`、动态裁剪 | 部分：Lobby/Play 可持久化；Combat 只从尚不存在的 `combat.active` 派生 | 公共 facade 启停权威战斗；每次阶段变化通知并允许下一次合法原生调用 |
| 权威随机流 | `dnd_dice_roll`、`dnd_check` 与状态同事务 | 完成基础层 | 随机流位置、收据、revision、精确幂等响应在重启后保持一致；并发调用不重复抽取 |
| 分支 | `branch_query/change` | 完成 | 公共 facade 覆盖 current/list/get/compare/create/checkout；revision、活动分支和幂等守卫；Lobby/Play 状态物化、精确重放与重启回归 |
| 快照与恢复 | `snapshot_create/restore/query` | 完成 | 公共 facade 覆盖 list/get/verify/lineage/create/restore；head、revision、活动分支和幂等守卫；真实 stdio 宿主恢复后刷新阶段并成功执行下一次合法原生调用 |
| revision、undo/redo | `state_revision` | 完成 | 公共 history/receipt/undo/redo；DM、分支历史游标、原子幂等响应、随机流状态撤销/重做与重启回归 |
| 事件与连续性 | `campaign_event`、`continuity_context` | 缺失 | 事件原子落账、受众事实、时间线、恢复后连续性重建 |
| 访问控制 | `access_grant`、campaign/actor scopes | 部分：grant 仍藏在 `campaign_change` | 独立任务型 facade；DM、玩家、私有 NPC、跨战役、调用时权限回归 |
| 角色基础 | `character_create_from`、metadata/state tools | 部分：create/update/query | CoC investigator/NPC/creature schema、模板实例、metadata 与状态结算均有 revision/幂等 |
| 物品与经济 | `inventory_change/transfer`、`wallet_change` | 缺失 | CoC 装备、武器、弹药、资产/信用评级相关确定性操作和原子转移 |
| 角色知识 | `actor_knowledge_query/change` | 完成基础层 | DM/玩家受众、错误信念、分支恢复、NPC 私有上下文回归 |
| 记忆 | `memory_query/change` | 完成基础层 | 分支隔离、revision 与恢复证据补齐 |
| 模组检索 | `module_search`、`module_expand`、`module_query` | 部分：一个复合 `module_query` | search 返回有界命中；expand 返回精确来源；玩家不见 Keeper 内容；活动版本唯一 |
| 技能/规则检索 | `rule_search/expand`、分层 `skill_query` | 部分：只能整文件读 skill，无规则 Pack | CoC 规则 Pack、outline/section/search/read、来源收据与战役规则上下文 |
| 有界评估 | `bounded_evaluation` | 缺失 | Agent 裁定请求有明确输入、来源、默认 resolver 和持久化收据 |

## Content Pack 与创作

| 能力域 | D&D 当前协议 | CoC 当前状态 | CoC 完成证据要求 |
| --- | --- | --- | --- |
| 当前 Pack schema | unified `sagasmith.content-package` v2 | 完成模块层 | CoC 模块 validator、archive round trip、跨战役导入 |
| Module Draft | `start/get/evidence/edit/finalize`；source/content/statblock/asset/actor/package/advance | 部分：功能面已具备 PDF 页证据、source/content/statblock/asset/actor/package/advance；尚缺真实私有 PDF 全流程证据 | CoC statblock 校验和中断后 advance 已通过当前 facade；真实用户 PDF 完成一次全流程 |
| 终结信任边界 | Agent 明确确认；最终档不可变 | 完成基础层 | 精确幂等重放、重启后不可变、新修改只能创建新版本 |
| Pack 管理 | list/get/import/export/activate/deactivate/remove | 完成 | 全部动作由当前 facade 提供；激活含 Agent 进度重映射，停用与删除具有原子精确回执，删除后仍可重放 |
| Pack 导入可恢复性 | MCP 负责结果与幂等 | 完成 | Pack checksum 区分候选版本；module/actor/asset/review/binding 逐步确定性收敛；演员绑定后故障注入证明重试不重复并产生精确最终响应 |
| Rulebook Draft | `rulebook_draft` | 缺失 | CoC 7e 规则书机械首遍、Agent 修订、规则 Pack 终结和私有源边界 |
| Content Solution | `content_solution` | 缺失 | 缺失/冲突内容通过来源、Pack 或 Agent 裁定解决，不能由 MCP 猜测 |
| ModuleGen | 当前 schema Pack 创作 Skill | 部分：通用 Skill 已升级，CoC 专用流程未接入 | Skill 通过真实 `module_draft` facade 构建一个用户 PDF 私有 Pack |

## CoC 规则域

| 能力域 | CoC 当前状态 | 缺口与完成证据 |
| --- | --- | --- |
| d100、难度、奖励/惩罚骰 | 完成基础层 | 增加角色技能读取、push、对抗、组合检定和原子状态结算 |
| Luck | 缺失 | 花费 Luck 修改检定、revision/幂等、下限和权限回归 |
| SAN | 部分：已有来源明确的权威遭遇结算 | 公共 `coc_sanity_check` 已把 SAN/损失/INT/bout 随机流与调查员状态原子提交，并覆盖权限、幂等、重启和 revision group；仍需每日重置、治疗恢复、潜在神话技能增长与连续性流程 |
| 伤害、重伤、濒死 | 部分：已有权威单次伤害/治疗结算 | 公共 `coc_hp_change` 已覆盖 HP、major wound、CON、unconscious/dying/dead、急救/治疗、随机流与精确重放；仍需濒死轮次、自然/周治疗调度和战斗 encounter 集成 |
| 战斗 | 缺失权威 encounter | start/end、回合、DEX 顺序、fight back/dodge、maneuver、枪械多发、弹药、掩体、Grid/Agent 空间模式 |
| 追逐 | 部分：纯解析器 | 权威 chase、MOV 排序、行动点、hazard/barrier、战斗互斥、结束/恢复 |
| 调查 | 缺失结算层 | 线索发现、明显线索不阻塞、花费 Luck/push、个人受众与秘密信息 |
| NPC 对话 | 缺失 | 每 NPC 隔离 worker、私有上下文、提案收敛、mechanic/场景变化前 close/abort |
| 角色成长 | 部分：development 纯函数 | session 结束成长、技能勾选、年龄、信用评级和 Pack 来源同事务 |
| 法术/典籍/神话 | 仅 Pack 目录字段 | 阅读时间、语言、神话技能、SAN、魔法值、法术学习/施放和来源收据 |

## UI、Skills 与回测

| 能力域 | CoC 当前状态 | 完成证据要求 |
| --- | --- | --- |
| Content Workbench | 缺失 | 展示草稿、来源证据、Pack 决策、验证、终结、导入、激活；对接真实 MCP/gateway |
| 调查工作区 | 缺失 | 场景、线索、手册、地图、调查员私有信息和 Keeper 控制视图 |
| 战斗/追逐工作区 | 缺失 | 权威状态、选择、掷骰收据、Grid/Agent 模式、恢复 |
| CoC Skills | 部分 | MCP 当前契约、零知识启动、调查、SAN、战斗、追逐、NPC、Pack 创作、恢复与双战役指导 |
| 私有 Pack | 缺失 | 用户五份 PDF 保持本地，生成 checksum 清单、私有 archive 和可提交的合成回归夹具 |
| 全模组回归 | 缺失 | 自动发现私有 Pack；每个可运行模组到达至少一个合法结局；明确机器可读 exclusion |
| 两个并行战役 | 缺失 | 两个独立 campaign/session/随机流/角色知识并行运行，均完成 Lobby→Play→Combat/Chase→Play→合法结局并可重启恢复 |

## 当前最短关键路径

1. 以真实私有 PDF 证明 Module Draft 与 Pack 导入全流程。
2. 实现规则书 Draft/Pack 与 CoC 规则检索，使 Quick-Start 能成为本地规则依据。
3. 实现 event、continuity 与面向玩家/私有 NPC 的受众结算。
4. 在已完成的 SAN/HP 单次权威结算之上，实现调查、战斗、追逐、NPC 对话和跨场景恢复流程。
5. 更新 CoC Skills 和 ModuleGen，然后用 The Lightless Beacon 做垂直切片、Alone Against the Flames 做图回归。
6. 对接 CoC UI，最后执行两个战役并行回测与完整完成审计。

任何一项从 `部分/缺失` 改为 `完成` 时，必须同时填入公共 facade 测试、真实宿主测试或回测产物；内部 service 调用不构成完成证据。
