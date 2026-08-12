# SagaSmith CoC 对标 D&D 的功能、契约与证据矩阵

本文件以 2026-08-13 的当前协议为基线。对标指概念能力、权威边界、
公共 facade、真实宿主行为和回归证据达到同等级；不复制 D&D 独有的
职业、法术位或空间规则，也不把来源/叙事判断错误地下沉到引擎。

## 完成结论

CoC 的共享全链路已完成：规则和 Module Content Pack 创作、战役运行时、
角色与长期状态、调查、SAN、Combat、Chase、隔离 NPC 对话、动态 MCP
工具、Skills、Keeper UI、重启恢复和两个并行私有战役均有公共证据。

| 能力域 | 当前 CoC 公共协议 | 状态与证据 |
| --- | --- | --- |
| 原生动态工具 | `exposure`、会话原生列表、`tools/list_changed`、调用时二次校验 | 完成；51 个当前工具，Lobby → Play → Combat → Play 的真实 stdio Host 会刷新并直接调用下一阶段原生工具 |
| 权威状态 | campaign/character revision、幂等、随机流、分支、快照、undo/redo | 完成；公共 facade、重启和真实双战役均验证 |
| 访问与受众 | campaign/actor grant、ActorKnowledge、事件、memory、`continuity_context` | 完成；Keeper、玩家、角色私有知识和 group/public 投影均在调用边界复核 |
| Module Pack | `module_draft(start/get/evidence/edit/finalize)`、`content_pack` | 完成；PDF 页证据、Agent 修订、不可变终结、跨战役导入、激活和私有 Pack 回测均验证 |
| 规则 Pack | `rulebook_draft(start/evidence/finalize)`、`rule_query(sources/search/expand/effective)` | 完成；Quick-Start 私有规则 Pack 已生成、导入、锁定、搜索和展开 |
| 规则/技能检索 | 分层 `skill_query`、规则来源和有效 lock | 完成；Skill 与规则 Pack 保持独立权威和来源收据 |
| 有界评估 | `bounded_evaluation(validate)` | 完成；签名 context receipt、严格 proposal、无工具 worker 与零权威写入均验证 |
| 角色状态 | template instantiate、inventory、wallet、技能成长、Luck、治疗、年龄、tome/spell study | 完成；写入均有角色/战役 revision、幂等和随机流收据 |
| CoC 判定 | d100、难度、奖励/惩罚骰、Push、Luck、组合/对抗、团体 Luck | 完成；调查待决选择和后果由公共 facade 原子结算 |
| SAN 与 HP | SAN loss/bout、伤害、护甲、重伤、昏迷、濒死、治疗 | 完成；即时机械转换由引擎拥有，跨场景时机由来源和 Keeper 明确提供 |
| Combat | start/query/action/attack/end，Grid/Agent 两种空间模式 | 完成；顺序、响应、dodge/fight-back/dive、围攻、弹药、故障、伤害和恢复均有回归 |
| Chase | start/query/action/end，人物和车辆参与卡 | 完成；MOV、Build、来源卡、路线、障碍、行动点、Combat 互斥和重启均有回归 |
| NPC 对话 | `npc_conversation` 与 host-local `npc_conversation_worker` | 完成；每 NPC 隔离持久 worker、Agent 受众事实、server-derived publication、close/abort 和阶段互斥均验证 |
| Skills | 51 工具契约、Keeper、campaign manager、工作流与恢复引用 | 完成；仓库 validator 强制工具集合和关键流程 |
| ModuleGen | Module 与 `core_rules` Content Pack authoring | 完成；当前 unified schema、Agent review/finalize 和规则 Pack 路径已接入 |
| Keeper UI | Content/Rules、调查、Combat/Chase、NPC Dialogue、调查员长期状态 | 完成；API 工具集合测试、Astro 检查、静态构建和浏览器实测通过 |

## 私有来源与真实宿主证据

私有 PDF、Pack 和逐调用日志仅保存在本地 `.runs/coc-private-v1`，不进入 Git。

| 证据 | 结果 |
| --- | --- |
| Quick-Start 规则 Pack | `coc7e.rules.quick-start.private` 1.0.0；两战役分别导入、激活、检索和展开 |
| The Lightless Beacon | 真实 stdio campaign 到达 `ending:survived-rescue`；81 次公共调用；Agent 空间 Combat；隔离 NPC 对话；重启后结局与公开 transcript 均存在 |
| Alone Against the Flames | 真实 stdio campaign 到达 `ending:escape`；80 次公共调用；Chase、Grid Combat、snapshot、branch、undo/redo；重启后结局存在 |
| 并行方式 | 两个同时运行的真实 stdio MCP session，各自拥有独立权威 campaign home，共享只读私有 Pack 来源，避免把本地 SQLite 当作多进程数据库 |
| 机器可读排除 | 当前两条合法结局路线没有来源要求的车辆追逐、tome/spell、therapy/aging；这些机械由公共 facade 测试覆盖，没有为回测虚构模组事实 |

## 权威边界（不是缺失能力）

- `module_query` 是当前统一模块导航 facade；不复制 D&D 的旧命名或兼容别名。
- Inventory 和 wallet 是角色本地原子状态。跨角色转移需要玩家意图和接收方授权，
  由 Agent 将已确认结果结算为相应角色写入，不伪造单一所有权事务。
- 车辆身份、MOV、Build 和 Chase 资源是权威状态；碰撞后果、复杂协助和追逐中
  未结构化空间事实由来源与 Keeper 明示，再通过现有伤害/行动 facade 结算。
- 战斗机动的来源目标和叙事效果、掩体几何、射击序列选择以及自然/周治疗发生时机
  属于 Agent/来源决定；命中、随机、伤害、护甲、弹药、状态和 revision 仍由引擎拥有。
- `content_solution` 不作为第二套解析/工作流协议存在。缺失或冲突内容按
  `Pack data → Skill procedure → system mechanic → core primitive` 处理，并可用
  `rule_query`、draft evidence 和 `bounded_evaluation` 留下可复核依据。

## 完成标准

完成状态必须同时有公共 facade 测试和至少一种真实 Host/回测证据；内部 service
调用不算完成。私有模组只覆盖来源实际要求的路径，未出现的来源事实必须记录为
机器可读 exclusion，而不能为了提高覆盖率而编造。
