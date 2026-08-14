# SagaSmith CoC 对标 D&D 的功能、契约与证据矩阵

> 当前状态（2026-08-15）：权限上下文、角色生命周期和 NPC conversation
> 已收敛到单一当前协议。本文取代 2026-08-13 的旧完成结论。

对标指概念能力、权威边界、公共 facade、真实宿主行为和回归证据达到同等级；
不复制 D&D 独有的职业、法术位或空间规则，也不把来源或叙事判断下沉到 Core。

## 当前结论

CoC 当前公开 50 个原生工具。`npc_conversation_transport` 是经过 Host token
鉴权且永不出现在 `tools/list` 的私有 transport，不是额外的公开工具，也没有
旧公开 worker 兼容入口。Agent 根据 server capability 私下调用 transport，
Director 只收到 server 验证后的 publication，看不到 capsule 或 raw proposal。

| 能力域 | 当前 CoC 协议 | 当前证据 |
| --- | --- | --- |
| 原生动态工具 | `exposure`、session-aware `tools/list`、`tools/list_changed`、调用时二次鉴权 | 公共 facade 与真实 stdio 覆盖 Lobby → Play → Combat → Play；phase、role 或授权变化后原生列表和下一次调用同步刷新 |
| Host 上下文 | domain、campaign、principal fingerprint、authorization fingerprint、role、audience、branch 共同形成 `context_epoch` | campaign revoke 和 actor-private 降级都会改变 epoch，向目标 session 发出 barrier；下一次私有读取失败 |
| 角色生命周期 | `character_change(create/instantiate/update)` | create/instantiate 原子写 actor、template lineage、初始 grant、幂等 receipt 和 lifecycle revision；update 具有角色 revision、幂等、undo/redo |
| 快照与分支 | snapshot schema 9、branch checkout、state revision | actor 与 actor grants 一同 capture/restore；公共测试覆盖 restart、snapshot/branch、create/update undo/redo 和同 ID 恢复 |
| 访问与受众 | campaign/actor grant、ActorKnowledge、事件、memory、`continuity_context` | Keeper、玩家、角色私有知识和 group/public 投影在调用边界复核；revoke 不只改变未来 ACL，也使旧宿主上下文失效 |
| Module / 规则 Pack | `module_draft`、`rulebook_draft`、`content_pack`、`module_query`、`rule_query` | 当前 public facade 覆盖 draft review/finalize、导入、激活、检索和来源收据；旧归档不享有兼容导入路径 |
| CoC 判定 | d100、难度、奖励/惩罚骰、Push、Luck、组合/对抗、团体 Luck | 调查待决选择、随机收据、revision refresh 和原子结算由公共 facade 覆盖 |
| SAN、HP 与长期状态 | SAN loss/bout、伤害、护甲、重伤、濒死、治疗、成长、Luck、年龄、tome/spell study | 即时机械转换由系统引擎拥有；跨场景时机仍由来源和 Keeper 明确提供 |
| Combat / Chase | Grid 与 Agent 两种 Combat 空间模式；人物与车辆 Chase 卡 | 公共回归覆盖响应、攻击、伤害、Combat/Chase 互斥、Grid/Agent、重启；私有两路线分别覆盖 Agent Combat 与 Chase → Grid Combat |
| NPC conversation | 公开 `npc_conversation` + 私有鉴权且不列出的 `npc_conversation_transport`；conversation v3、proposal v4 | 真实 Agent + 实际 CoC stdio MCP 回归验证 capability、隐藏 transport、私有 capsule、publication；公共 facade 覆盖 close/abort、阶段互斥和 stale authority |
| Skills | 50 个公开工具、conversation v3/proposal v4、Host 私有 transport | `SagaSmith-coc-skills` validator 强制当前工具集合和关键流程；不含公开 worker 契约 |

## 私有来源与真实 Host 证据

私有 PDF、Pack、临时数据库副本和逐调用日志只保存在本地 `.runs`，不进入 Git。
2026-08-15 的当前运行报告是
`.runs/coc-private-current-20260815-run5/reports/parallel-campaign-backtest.json`。

| 证据 | 结果 |
| --- | --- |
| The Lightless Beacon | 当前 CoC stdio runtime 从保留数据库的临时副本恢复私有 Pack，71 次调用到达 `ending:survived-rescue`；DM/player 投影、隐藏鉴权 transport、bounded evaluation、Agent 空间 Combat、Play 恢复和重启后结局/公开 transcript 均通过 |
| Alone Against the Flames | 当前 CoC stdio runtime 从独立临时副本恢复私有 Pack，64 次调用到达 `ending:escape`；DM/player、Chase、Grid Combat、undo/redo、Play 恢复和重启均通过 |
| 当前 Agent transport | `SagaSmith-agent/tests/agent/test_npc_conversation.py` 启动实际 CoC stdio MCP，通过 capability 构造私有 transport；`tools/list` 不列出 transport，Director 不接收 capsule/raw proposal |
| 数据安全 | 原 `.runs/coc-private-v1` SQLite 与 Pack 均未改写；两条路线使用彼此隔离的数据库副本和共享只读 Pack 来源 |
| 旧归档边界 | 旧 `.sagasmith-pack` 的 legacy scene metadata 被当前单一 schema 预检拒绝；没有为让旧回测导入而增加 alias、双读、fallback 或弱化验证。当前路线复用临时数据库中已经导入并激活的不可变 Pack |
| 机器可读排除 | 报告记录两条合法结局路线没有来源要求的车辆追逐、tome/spell、therapy/aging；相应机械由公共 facade 测试覆盖，没有虚构模组事实 |

私有路线证明当前 CoC runtime 与私有 transport 能在真实模块状态上继续运行；
当前 Agent 测试另行证明实际 Host 的 capability/transport 隔离。二者与 public facade
回归共同构成证据，任何内部 service 测试都不单独算完成。

## 权威边界

- `module_query` 是唯一模块导航 facade；不复制旧命名或兼容别名。
- Inventory 和 wallet 是角色本地原子状态。跨角色转移需要玩家意图和接收方授权，
  由 Agent 把已确认结果结算为角色写入。
- 车辆身份、MOV、Build 和 Chase 资源是权威状态；复杂协助、碰撞后果和未结构化
  空间事实由来源与 Keeper 明示，再通过机械 facade 结算。
- Agent 空间模式不制造坐标；来源目标、掩体几何、射击序列选择和跨场景治疗时机
  属于 Agent/来源决定。命中、随机、伤害、护甲、弹药、状态和 revision 仍由引擎拥有。
- 缺失或冲突内容按 `Pack data → Skill procedure → system mechanic → core primitive`
  处理。旧 Pack 若不满足当前 schema，应创建新 draft/version，而不是给 runtime 增加兼容读取。

## 完成标准

完成状态必须同时具备公共 facade、真实 native tool refresh 和至少一种真实 Host 或
回测证据。私有模组只覆盖来源实际要求的路径；未出现的来源事实必须记录为机器可读
exclusion，不能为了覆盖率编造。
