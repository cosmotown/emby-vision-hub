# 人物治理第二阶段：进展与必须先确定的补偿边界

> 本文保留第一批完成时的历史设计与暂停点。2026-10-14 的最终 B+C 策略、
> existing-only 创建边界、文件账本及验证结果见
> [第二阶段实施报告](person-governance-phase2-report.md)。旧文中的“待确认/未实现”
> 不代表当前实现状态；没有执行本文提出的新 PG 表 migration。

基线：v7.2.36，源码树 `b40736a1d88d564a860cb94411255ed886e88611`。
开发分支：`fix/person-governance-phase2`。未提交、未发布、不操作 NAS。

补充：[上游 Person/Actor delta 审计](person-governance-upstream-delta.md)。已确认 Episode guest 向 Series 晋升、name-group rename、cap 时序等余留问题；上游白名单模式不能原样移植。本补充仅为审计和实施建议，不代表补偿策略获批或第二阶段已完成。

## 已实现的第一批防线

- 人物枚举携带 `full_snapshot / partial_page / filtered_result / incremental_result / unknown_completeness` 分类。
- 全量完成要求页结构正确、Id 不重复、计数一致、尾页结束及最终总数复查。不把空页、HTTP 429/500、未知总数当作已完成。
- `full_snapshot` 仅表示本次分页读取证据完成，不是服务器事务快照：同数量的并发替换仍无法由计数发现。因此当前人物映射同步 **所有 scope 均仅增补，缺失只报告不解绑**，没有利用它开放 destructive reconcile。
- 普通 upsert 不再覆盖已有不同 Emby/IMDb/Douban ID；SQL ON CONFLICT 也有条件约束，不能仅靠 Python 预检。
- 演员数据补充不再因 IMDb 冲突迁移身份/删除源映射，不再因查询未确认就清空 TMDB。
- 共享导入不再合并多条既有映射。单个稳定 ID 精确命中且其他已填 ID 无冲突时，只允许补空字段；多行、同名、空 ID、跨 provider 冲突全部报告。
- 共享导入返回 `review_items` 和计数，日志报告冲突；**这还不是跨重启的持久 review queue**。

未改 core_processor 的临时人物创建/失败 detach。未新增 DeletePerson 调用，未修改既有删除许可。未实现完整 protection adapters，不能宣称整个第二阶段完成。

## 为什么不能直接加“查零引用后 DeletePerson”

当前创建方式不是 `CreatePerson` 返回一个带归属证明的新 ID，而是给媒体 People 提交一个临时名字，再读回服务器解析的 Person。读回新 Id、名字相同或创建时间很近，都不是服务端签发的本操作独占归属证明。

还有两个独立窗口：

1. Emby POST 成功之后、Python/PG 记住 Person ID 之前崩溃。局部 context 已丢失，不能靠后来扫描到的同名对象自动回收。
2. 全部 GET 都是零引用，之后其他任务或 Emby 客户端新增引用，再发送 DeletePerson。现有删除封装只有 person_id，没有“引用版本仍为 X/仍无引用才删除”的原子条件。

PG advisory lock 能约束合作的 EVH 任务，不能锁住 Emby、插件或第三方客户端的写入。不能把 advisory lock 宣称为全局无并发证明。

所以仅增加运行时 `created_by_this_operation=True` 并不能满足进程崩溃恢复和强零引用不变量。也不能通过调整故障测试只测顺序执行来假装满足。

## 推荐设计（待确认后实施，不执行 migration）

### 1. 持久化最小 operation ledger

建议一张新表 `person_supplement_operations`，不改 Emby schema，不回填历史人物来源：

| 字段 | 语义 |
|---|---|
| operation_id UUID PK | 先于任何 Emby 写入落库 |
| source | tmdb/douban/manual/sync/maintenance/legacy_supplement |
| parent_media_id | 当前补充目标 |
| provider / provider_id | canonical 单 provider 身份 |
| person_id nullable | 已确认时才填写；未知不能猜 |
| ownership_state | existing / unconfirmed / created_confirmed；默认 unconfirmed |
| state | prepared / submitting / attached / complete / needs_review / interrupted |
| intent_hash / before_relation_hash | 记录准备的媒体关系版本和操作 intent，不存 token/raw headers |
| attempt_count | 写入前预留；响应不确定不能自动重发 |
| created_at / updated_at / last_error_code | 持久诊断，不存 API key/密码 |

可为 active `(source, provider, provider_id)` 设置唯一约束；需要处理同一人物关联多个媒体的等待/复用，不可将锁冲突当作人物不存在。

Migration：additive `CREATE TABLE IF NOT EXISTS` + 必要索引；不改现有 Person/cleanup 表，不重写历史记录。第二次启动幂等；重启只把不确定操作送 `interrupted/needs_review`，不自动删除或重复 POST。尚未创建这张表。

### 2. 失败时优先 fail-closed，不制造无主对象

推荐：能复用就复用；读取失败≠不存在；所有 provider/头像/元数据准备尽量在创建前完成。

新对象创建后如果补充失败，而且无法安全回收：**保留该操作的最后有效媒体关联，标记 needs_review，不自动 detach 后留下孤立人物，也不凭零引用 GET 自动删除**。这是需要确认的产品行为变化：失败时可能暂留待修复的演员项，而不是看起来完全回滚。

如果要求“失败必须撤销最后关联，且人物必须立即消失”，则需要原生/插件提供可证明对象归属与条件删除的原子能力；EVH 现有 GET + DeletePerson 无法独立保证。这不是可以用另一次 GET 补齐的小修复。

响应丢失/创建归属不确定时只 HOLD。绝不扫描所有 orphan 来补偿，不操作其他 operation 对象，不把接近的 created_at/同名当归属。

### 3. 统一只读 protection result（待实施）

建议返回结构而非只有一个 list：

```text
person_id
reasons[]
coverage {source: complete / unknown / not_applicable}
observed_at
evidence_generation
identity_evidence（独立字段）
destructive_allowed = false if any reason or any unknown
```

必须配套的 adapters：

- 实时媒体 People exact reference、保护库 ID/name/provider/alias；不依赖 PG cast 缓存证明无引用。
- actor_subscriptions，包含停用/暂停但仍存在的记录。
- user_media_data、Emby 用户 favorite、收藏/playlist、媒体 watchlist 到父人物的精确关系。
- custom_collections.definition_json 与其他规则中的人物引用。
- 正在运行及排队任务、pending webhook、metadata/翻译/supplement operation ledger。
- 人工保护：当前没有已证明完整的 Person denylist；不能用“表不存在”冒充 not_applicable。应另建最小名单或选定已有正式配置来源。
- 其他业务记录/插件引用；无法读取或解释时 unknown，不让零计数放行。

仅历史 completed cleanup/proof 记录应作为证据，不永久视为正在占用人物；delete_attempt 等安全账本不能随扫描删除。

各 delete/merge/compensate 入口最终只调用这一个模型；在模型尚未齐全之前，不把当前第一批变更当作删除批准。

### 4. review queue 的持久化

当前补丁停止了 destructive merge，但 `review_items` 仅随结果返回和写日志。建议冲突记录纳入 operation/review ledger，或另加小型 `person_identity_reviews` 表，按稳定冲突指纹去重；不得借用 cleanup candidates 充当 merge queue。

这项 migration 同样应先确认，不能宣称日志就是可靠的跨重启队列。

## 测试状态与未完成 gate

- 第一批 targeted：原 49 项 + 新同步/身份单测 20 项 + 专用 PG 5 项，共 74 项通过。
- PG 用全新内部网络与临时容器；每个测试只操作 connection-local TEMP tables，没有现有 lab/生产数据写入。
- 全后端 discovery：689 项，564 通过、125 跳过（未配置相应外部/PG fixture）；不能写成 689/689 全通过。首次只读容器运行因依赖缓存目录不可写发生 1 项导入错误，增加容器临时缓存挂载后重跑通过，没有改依赖业务代码。
- 本次修改 compileall、git diff --check 通过。临时 PG 容器、其合成数据卷及内部网络已移除；可由测试重新生成。
- 两个旧故障模型的断言本来就是“当前会留下 orphan / 会生成重复人物”。它们在修复后必须改成相反的安全断言，不能一边要求 bug 消失一边要求旧 bug 断言仍 PASS。第一阶段原始证据应保留，不覆写。
- `no orphan growth`、create 后 crash、并发使用、完整保护覆盖以及第二阶段真实生命周期 E2E 还未通过，不能进入第三阶段生产 inventory 或发布。
- 为避免把“数据库行数没减少”误当安全，还测试已有 Emby ID 没被替换、actor_metadata 没因 merge 级联丢失。

下一步需要确定：接受“持久 ledger + 无法安全补偿时保留最后关联并待复核”，还是坚持强制完整回滚并先增加服务端原子能力。未确认前不把不安全补偿删除接进产品。
