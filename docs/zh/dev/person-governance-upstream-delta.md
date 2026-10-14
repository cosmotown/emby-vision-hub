# Person governance Phase 2 — upstream delta（只读对照）

核验日期：2026-10-05。当前 EVH 基线 `b40736a1d88d564a860cb94411255ed886e88611`，分支 `fix/person-governance-phase2`，包含第一批未提交防线。本文不代表整个第二阶段通过。

上游：**hbq0405/emby-toolkit**，此次通过 GitHub API 与独立浅克隆双重核验的 main 为 `3b7ea3df77cba2d1b0ffec844aabf356e762e69b`，committer 时间 `2026-09-26T16:55:22Z`。不改用 README 推荐的 ETKvNext 作为审计对象，不 cherry-pick、不运行上游应用或 migration。

固定源码入口：[upstream core_processor.py](https://github.com/hbq0405/emby-toolkit/blob/3b7ea3df77cba2d1b0ffec844aabf356e762e69b/core_processor.py)、[tasks/actors.py](https://github.com/hbq0405/emby-toolkit/blob/3b7ea3df77cba2d1b0ffec844aabf356e762e69b/tasks/actors.py)、[actor_utils.py](https://github.com/hbq0405/emby-toolkit/blob/3b7ea3df77cba2d1b0ffec844aabf356e762e69b/actor_utils.py)、[database/connection.py](https://github.com/hbq0405/emby-toolkit/blob/3b7ea3df77cba2d1b0ffec844aabf356e762e69b/database/connection.py)。下列行号指本次读取的源码，不保证将来仍相同。

## 1. Series cast 差异与真实调用链

EVH `core_processor.py:92` `_aggregate_series_cast_from_tmdb_data`：

1. 先取 `series_data.credits.cast`，以 actor ID 建 map。
2. 遍历全部 `episodes_details`，把每集 `credits.cast + credits.guest_stars` 加入同一 map。
3. 按 order 排序。缺 order 的分集条目被原地补 999；不会区分主剧排序和分集排序的语义。
4. 入口有 `:596`（实时监控数据处理）、`:2047`（主处理 fresh-data 分支）、`:2204`（元数据补全分支）。无 aggregated data 的 fallback 取 Series `aggregate_credits/credits`，不是所有路径都聚合集客串。
5. authoritative cast → `_process_cast_list:2580` → provider 解析/去重/头像检查 → cap `:3141` → 翻译/格式化 → PG `upsert_person:3309`。
6. 主流程 `:2349` 写 metadata override → `:2362` 附近刷新 → `:2369` 豆瓣独立人物创建/复用。`_build_cast_from_final_data:4734` 将最终 TMDB cast 写到媒体数据；Emby/plugin 消费这些数据时可能创建或复用 Person。PG upsert 本身不是 Emby CreatePerson。

上游 `_get_series_main_cast_from_tmdb:84`：只看当前 Series 的 `credits` 或 `aggregate_credits`，取 cast 并按 order 排序；不接收 Episode 参数。主处理在 `:2614/:2678` 调用，翻译前后均重新取 Series main cast。

借鉴边界：使用 Series 自身 credits 作为权威源；aggregate_credits 仅为 Series 级 fallback，不再把 Episode records 合并进去。但上游 helper 本身没有完整 provider 冲突检查/身份去重；不能替代 EVH 的安全 resolver。未知或不完整 Series credits 不能被当成“空 cast 已证实”，进而自动清空既有关系。

## 2. EVH 确实仍向 Series 晋升 Episode guest stars

审计测试只提取实际 AST 函数，使用合成数据、内存 DB mock、禁用 AI/在线 provider，不导入应用初始化、不访问 Emby。

| 样本 | Series 自身 | Episode cast 唯一数 | guest 唯一数 | EVH 聚合输入 | 当前最终 Series 处理 | 改用 main-only 后 |
|---|---:|---:|---:|---:|---:|---:|
| 12 集；每集 4 位独有 cast + 3 位 guest | 8 | 56 | 36 | 92 | 50 | 8 |
| 12 集；cast 只有相同 8 位主演，另有 3 位 guest | 8 | 8 | 36 | 44 | 44 | 8 |

三列演员数有交集，不能直接相加。每项带有效合成 ID、头像；cap=50，无豆瓣额外演员。运行实际 EVH `_process_cast_list`，DB/formatter 使用 mock；真实 selection/filter/dedupe 函数参与计算。对应 mock mapping upsert 次数为 50→8、44→8。

结论：这是**明确的父 Series 人物处理范围扩张来源**。第二个样本 36 个纯 guest 都进入父 Series。不能据此宣称生产减少 36/42 个 Person，或推算八万人的来源比例：同一 Person 可能在别处已有引用/已存在，Episode 自身仍有合法演员需求。未读取生产样本。

## 3. 取消聚合能减少什么，不能减少什么

- 减少 Series 级 provider 解析、头像补全、翻译、映射 upsert、Series metadata cast 写入，以及由这些写入带来的 Person 创建/复用尝试。
- 不再因单集 guest 的局部 order 很靠前就抢占父 Series main cast 名额。
- 不取消 Episode 自己的 cast/guest stars；不将 Episode/Season 的演员数据提升到 Series。
- 不自动减少/删除已经存在的 Emby Person；不依据本次 main-only 结果做全局 reconcile。
- 豆瓣 Series cast 补充是另一路来源，不会仅靠取消 TMDB 分集聚合自动消失，仍需限定 Series 来源语义和精确身份。
- EVH 缓存/override 中可能已有旧聚合结果；首次实现必须审计缓存恢复分支，防止 main-only 查询后又用旧结果重新扩大新操作目标。历史缓存不应直接批量重写或删除。
- 未来 metadata 写回若包含更短 cast，可能改变该媒体关系，不能把它冒充无副作用读取；应单独测试并遵守完整性/保护/operation 所有权合同。

## 4. Provider whitelist 值得借鉴，但上游 helper 不能原样移植

上游 `_update_emby_person_names:2103`：

- 以 final_cast 建 `allowed_actor_tmdb_ids`；有 TMDB ID 的演员才进入重命名集合。
- 也允许当前 Emby People 的 TMDB IDs；导演/主创不受演员 cap 限制。
- TMDB → 当前 People 的 Emby ID / final_cast.emby_person_id → `AnyProviderIdEquals=tmdb.X` 查询。
- 生成 `emby_id_to_new_name`，批量取旧 Name，只有名字不同才 POST。

可借鉴的是 **provider target whitelist、将目标选择与写入分开、仅写 diff**。不要照搬“当前 People 自动扩大白名单”以及 crew 不经独立授权就扩大 mutation 范围。

本次验证发现的限制：

1. 上游全仓 Python AST 未发现这个 helper 的直接调用点；全文搜索也只有定义。不能把存在的 helper 描述成“上游所有 mutation 已启用白名单”。动态外部调用未证实。
2. 全局查 provider 使用 `Limit=5`，命中后首个 exact ID 即 break；重复 provider 对象不 fail closed。
3. final_cast 自带 Emby ID 会直接使用，写前批量 GET 只取 Name，不对当前 provider 再核验。
4. 没有 operation ownership、持久冻结目标集或服务端原子条件保证；GET+POST 仍有竞态。
5. 上游全局翻译 `tasks/actors.py:145/:260` 同样仍按 name group 扇出。

内存测试：同名张三 A/TMDB100、B/TMDB200，仅 final_cast100 时上游 helper 只写 A，B 完全不变；但重复 TMDB100 的 A/C 会选首项；陈旧 final_cast(100→B) 而 B 实际 TMDB200 仍会写 B。后两项是**缺口复现成功**，不是安全验收 PASS。

建议 EVH 的统一 mutation target 契约：

```text
本操作明确 provider identity intent
→ 完整 exact resolver（重复/冲突/不完整/读失败均 HOLD）
→ 冻结 {server scope, provider IDs, Emby Person ID, intended fields,
        before fingerprint, source parent media, operation_id}
→ 每次写前核验当前 exact detail 与冻结身份一致
→ 只允许目标中的字段差异写入；写后复核
```

provider stable ID 不是“拥有整个 Person 的所有字段”的授权。只允许该操作已授权字段，不能因为有 TMDB 就随意覆盖冲突 Douban/IMDb；没有 provider 的既存对象不按名字新增写目标。这里只提出接口合同，尚未新增产品 helper。

## 5. 当前 EVH mutation 路径余留风险

| 路径 | 当前依据 | 本阶段结果/待改 |
|---|---|---|
| 全局 rename `tasks/actors.py:2476/:2625/:2639` | name → 多 Person | 明确缺口。实际写计划 AST 用同名 Zhang San、100/200，生成 A/B 两个目标；应改成稳定身份 intent，不允许名字决定目标。中文张三本身被该任务前置中文检测跳过，不用中文样本伪造可达性。 |
| 手动 rename `core_processor.py:3850/:3870` | enriched People 的 TMDB → Emby ID | 比 name 安全；但缓存映射可能陈旧，同 provider 多对象字典覆盖，缺写前 expected-provider 合同。 |
| 通用 `handler/emby.py:578` | caller person_id，GET 再 POST | GET 没有核验 expected provider/type，不能单独充当 exact identity resolver；应由统一 mutation gate 控制调用。 |
| relation supplement `core_processor.py:3324` | Douban ID、provider 查找、临时名字读回 | 有重复 provider 拒绝，仍存在读取失败当不存在、临时名字确认所有权不足、陈旧关系覆盖等问题。 |
| rollback `core_processor.py:3340` | Id 或临时名字，重写 People | 无持久 ownership；不能证明最后 detach 不留 orphan、也不能证明零引用 GET 后可安全删除。继续等待补偿策略决定。 |
| provider bridge `core_processor.py:2860` + safety resolver `:137` | 名字/别名候选 + 唯一生日 | 不是纯 name，但也不是 exact provider bridge；同名同生日不能证明跨 provider identity。当前会 confirmed 后合并 Douban；推荐降为待复核证据，不授权自动 identity mutation。 |
| `find_person_by_any_id:141` / enrich `:166` | 逐字段首个匹配 / 以 Emby ID 用 PG 补 ProviderIds | 精确 SQL 不等于冲突已统一排除。first match 未证明多 provider 交集一致；陈旧映射需实时重验，不把 Emby ID 当 canonical。 |
| map upsert / maintenance / metadata enrichment | provider ID | 第一批已加冲突拒绝、停止自动 merge/删源映射；不能宣称其他入口因此已统一安全。 |
| merge candidate / orphan cleanup | 另有安全合同 | 本轮不开放、不移植上游 destructive 逻辑；匹配/证明不等于删除许可。 |

不能报告“所有 mutation 都已统一”。目前只完成部分 PG 防线，其余是审计定位与下一步设计。

## 6. Actor limit：cap 成立的范围及执行顺序

- EVH 正式字段为 `max_actors_to_process`，`constants.py:113-114` 默认 50。主处理 fallback 为 30（`:2740/:3141`），不是一律 50；`series_actor_limit / episode_actor_limit / DEFAULT_ACTOR_LIMIT` 这些名称在本次 EVH 产品 Python 源码中未出现。
- `_process_cast_list` 最终 `select_cast_by_source_order` 为稳定顺位排序后切片。合成输入 0/1/8/50/92 分别得到 0/1/8/50/50，没有 padding。
- 低于上限时确实有“补充模式”日志；其候选来自当前媒体豆瓣 cast，不是为了凑 50 搜索任意人物。高于 limit 也会继续核验候选，最后截断。不能仅看日志就断言强制补满。
- 顺序不足：豆瓣 celebrity detail、IMDb→TMDB 查找、部分 TMDB profile/头像补全在 cap 前；最后翻译、PG upsert、主 cast 写入/豆瓣挂载在 cap 后。**未发现先把全部 cast 创建为 Emby Person 再截断的这条主调用链，但已有昂贵读取先于 cap。**
- Episode override `core_processor.py:5030-5053` 的 cast/guest lists 仅按头像过滤，不走同一个 actor cap；Season/Episode fallback 会继承 Series cast。手动编辑提交列表也不是同一自动 cap 入口。因此“所有路径都统一 cap”不通过。
- 上游增加 `max_episode_actors_to_process`，默认 0 表示继承 Series，而非不限量；`:1462` 大于零才处理分集演员。**不直接复制默认 0 的产品策略**，它不等于用户要求 Episode 独立维护 cast/guest。

建议顺序：provider evidence resolve → 冲突拒绝/身份去重 → 稳定 source order → cap → 昂贵头像/翻译/Person 操作。用于证明身份所必需的 provider 查询可在 cap 前，但必须与昂贵 enrichment 区分；不要把需要补头像的高顺位者未经处理就错误排除。处理失败可保留不足 cap，不无限找人填满。

## 7. 可安全借鉴的部分

仅在 EVH 自身测试下重写小范围逻辑，不 cherry-pick：

1. Series 自身 main cast，Episode 自己维护 cast/guest；完整性未知不得清空旧关系。
2. 用稳定 provider identity 构造明确 target whitelist，mutation 与候选检索分层；名字仅展示/检索，不作写目标授权。
3. 顺位、去重、cap 在昂贵 enrichment/写入之前；针对 Series 和 Episode 分别验证，不更改默认数量来掩盖膨胀。
4. 只写已授权字段实际 diff；读取失败不能当空值/不存在。
5. canonical provider 身份和实例映射分开理解。模式已经在合成函数测试中得到部分验证，不代表上游端到端运行安全已验证。

## 8. 禁止移植的上游内容与数据模型差异

禁止：

- `tasks/actors.py:309` 自动合并，转移关系后在 `:497` DeletePerson；`:615` 其他清理调用。不能把引用转移成功当作完整保护/独占删除证明。
- `actor_utils.py:428-448` 跨行 ID 搬迁、清第三方 ID、删除源 person_metadata；`:465` 对无效查询结果删映射。
- `_premerge_douban_roles_into_tmdb_cast:2040` 用名称对齐后赋 Douban ID；会削弱 EVH 已有 provider 防线。
- provider 查询取首条、不证明枚举完整、按 name 批量写、读取失败继续写；以及任何缺 operation ownership/frozen targets 的回收。
- 上游 schema 清理/迁移：`database/connection.py:387` 单表 person_metadata，以 TMDB 为 PK；`:1214` 左右将 emby_person_id 列列为 deprecated 并 DROP。不得带入 EVH。

EVH 当前关系：

```text
person_identity_map.map_id           本地记录键，不是外部身份
  ├ tmdb_person_id / imdb_id / douban_celebrity_id  各 provider 身份证据（各自 UNIQUE）
  └ emby_person_id                   当前对象映射（UNIQUE，但没有实例/生命周期维度）
actor_metadata.tmdb_id               TMDB 元数据，FK→上述 TMDB，ON DELETE CASCADE
```

现有单行 schema 混放身份和对象映射，无法自然表示同 canonical identity 的多个 Emby 对象/重建后的生命周期；不能据此自动合并对象或覆盖旧 ID。Douban-only 是 EVH 现有需求，不能照抄 TMDB 必填 PK schema 丢掉它。

当前可不迁移：运行时采用 provider identity 作为 intent，对实例 Emby mapping 只作候选，写前核验；冲突 HOLD。若后续确需持久多对象映射，先另行设计 `canonical identities` 与 `(server identity, person_id, observed provider fingerprint, mapping state)` 分离关系、backfill/历史保留/冲突策略，获得确认后再 migration。这个方案不与待确认 operation ledger 混为一次静默变更。

## 9. 是否纳入当前第二阶段

建议 **纳入防新增部分**，否则第二阶段仍会保留明显膨胀入口与按 name 写目标：

1. Series main-only（含三个调用点、fallback、缓存恢复），Episode 不晋升父 Series；不处理现有人物删除。
2. 单一 exact identity target gate；先覆盖全局 rename、手动 rename、provider/metadata 补充和 relation supplement；冲突/模糊/不完整只复核。
3. 自动 cast cap 前移到昂贵 enrichment 之前，Episode 独立边界与手动路径范围明确。不能把 cap 变成 fill target。
4. 独立补充同名100/200、重复provider、陈旧Emby映射、读取失败、旧聚合缓存回流的产品回归测试。

暂不纳入：上游数据库迁移、自动 merge、destructive reconcile、孤儿回收。创建失败补偿仍等待确认 [持久 ledger + 无法证明安全回收时保留最后有效关联并待复核](person-governance-phase2-design.md)，本报告不是对该产品决策的替代批准。

## 本轮执行与验证边界

- 新增独立审计脚本 `ghost-person-audit-20260930/test_upstream_delta.py`（仓库外审计目录），9/9 characterization tests 通过。测试只执行选定 AST/pure functions，HTTP/DB 均 mock；首次 fixture 缺少 processor 的连接属性，补齐合成属性后全通过，未修改产品代码。
- 其中两项明确断言上游危险行为仍存在；这不是 production-safe 通过标志，未来产品回归应断言相反安全结果。
- 当前 EVH 同名身份保留、cap 与第一批同步安全单测另外重跑：`tests.test_actor_enrichment_safety` + `tests.test_person_governance_sync`，35/35 通过。使用源码只读、network=none 的临时容器；429/500 是故障 mock，不是真实 Emby 请求。没有重跑 PG/full backend，此前 74 targeted 与 564 pass/125 skipped 仍是上一轮结果。
- 审计脚本语法 compile、git diff --check 通过。
- 本轮只增加报告及仓库外审计脚本，没有修改现有产品源码、迁移或正式测试。第一批未提交改动原样保留。
- Emby HTTP=0，Person mutation=0，数据库连接/写入=0，NAS 访问=0；无 commit/push/release。
