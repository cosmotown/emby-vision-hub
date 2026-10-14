# Person governance 第二阶段实施报告

日期：2026-10-14。分支：`fix/person-governance-phase2`。
保留暂停时 HEAD `b40736a1d88d564a860cb94411255ed886e88611` 及第一批未提交修改。
本轮没有提交、推送或发布，没有操作 NAS，没有执行 Person 删除或 merge。

## 1. Episode guest → Series 的原根因

`_aggregate_series_cast_from_tmdb_data` 原来将 Series credits.cast、
所有 Episode credits.cast 和 guest_stars 合并为一个 map。8 位 main +
36 位单集演员会形成 44 位父 Series 处理目标。旧 override/DB cache 也能绕过在线取数，
重复使用这种聚合结果。

## 2. main-only 新算法

父 Series 只以 Series 自身 `credits.cast` 作为主人员范围；不以 episode 聚合或
aggregate-only 数据替代主人员证据。旧缓存先与当前主人员 provider ID 集相交。
当前媒体已关联人物先批量读取实例 ID → provider 映射，再按主人员范围、稳定顺位、
provider 身份及实例 ID 排序、去重、限额，之后才进入昂贵补充。

未处理的历史关联不作为新 enrichment 目标；写文件时保留可精确表示的既有关联。
不能安全表示的既有关联、无法读取的身份、超过处理预算的遗留关联集合会 HOLD，
不会通过截断旧关联制造 orphan。

## 3. Person 写入口及门控

| 入口 | 原调用链/介质 | 现在的处理 |
|---|---|---|
| global translation | 人物枚举 → 姓名分组 → Person POST | 每个冻结 provider target 独立翻译、独立复核 |
| manual edit | 编辑页 → API → media task → Person POST | UI 携带实例 ID/provider snapshot；API 校验格式和 cap；worker 写前复核 |
| Person metadata helper | GET 后合并任意 payload → /Items/{id} | 必须提供 ExactPersonTarget；逐个冻结 provider pair 唯一解析；重新 GET |
| generic item update | /Items/{id} | Type=Person 拒绝绕过 Person gate |
| Person image upload | 下载头像 → /Items/{id}/Images/Primary | 精确 target + user_id；上传前再读取/复核 |
| Douban relation supplement | People 临时姓名创建 → 绑定 ID → 补图/映射 → 失败解绑 | 仅复用精确已有对象；持久 reserve；验证关系；失败不解绑、不删除 |
| TMDb override | 父/季/集 JSON → 外部 metadata reader → Person 创建/关联 | 仅输出精确已有人物；写前重读；raw crew/created_by 也不能创建新人物 |
| cache-only / metadata webhook | 重写旧 JSON → 后续刷新读取 | 复用同一 metadata gate；缓存姓名不能直接发起 rename |
| pre-ingest | pending 媒体生成 override | 人物部分延后，保留非人物元数据处理；不输出可触发创建的 raw cast |
| generic metadata refresh | 解锁 DTO POST + Refresh | 明确 Person 类型拒绝；媒体刷新语义保持 |
| DB sync / import / maintenance | 实例映射与跨 provider bridge | 仅增补；冲突 HOLD；不解绑、迁移、自动合并或删除 |

现有 Person Cleanup/DeletePerson 代码未开放新资格，未新增 delete 调用。
普通媒体导入的 scan/refresh 属于媒体生命周期操作；现有调用点没有以 Person ID
执行该导入刷新。新设计也不提供一个通用 Person 创建 API。

## 4. 原 Name 写风险

删除了 global translation 的 name-group write fan-out、Douban 临时姓名创建/失败 detach、
按姓名/生日确认跨 provider 身份的分支，以及手动修改中用旧 TMDb→Emby map 直接写的路径。
Name 仍用于显示/翻译输入及拒绝性的冲突检查，不能选择或授权 mutation target。
同名冲突查询只能拒绝，不能替代 provider 身份证明。

## 5. ExactPersonTarget

冻结 dataclass：实例 person ID、provider_name、provider_person_id、source_item_id、
source_item_type、display_name、完整支持的 provider snapshot、operation_id。
canonical evidence 是外部 provider pair；Emby ID 仅表示当前实例中的对象映射。
无身份的诊断 context 拥有空 provider 集，永远不能通过写门控。

## 6. write-time revalidation

重新按每个冻结 provider pair 查询，必须唯一解析到同一个实例 ID；
再 GET 当前 Person，比较 Type/ID/provider snapshot。缺失、改变、歧义、
不完整查询和旧映射均拒绝。禁止通过 payload 改 ID/Type 或未经证明地改 ProviderIds。
metadata 文件写前再核对全部人物与当前姓名，阻止旧别名被外部读者当成新人物。

真实 Mac 验证确认 `AnyProviderIdEquals` 使用 `tmdb.287` 点号形式。
Emby 4.10.1.0 的韩文姓名搜索会返回 Items 非空但 TotalRecordCount=0；
姓名冲突检查不把 0 当作不存在，而是要求有界结果、明确空尾页、稳定重复首批。
不完整、超出 200 个候选或发生漂移时拒绝。原全量人物枚举的严格 count 合同不变。

## 7. cap 时序

provider 范围 → 精确身份去重 → 稳定顺位 → cap → enrichment/translation →
写入资格复核 → mutation。Source People 的 provider 映射用有界批量 GET 读取；
昂贵的逐人补充只处理限额后的目标。Episode cast/guest_stars 共用一个限额，
保持本集归属；不会提升到父 Series。8 位 main 不会主动补满 50。
手动 API 超过配置 cap 会拒绝，不静默截断请求。

## 8. 创建全调用链及现行支持边界

旧 Douban 链是 media People POST 临时姓名 → 读回 PID → 绑定 Douban →
改名/媒体关联/DB mapping/头像；中间失败后 detach 会留下孤立人物。
另一个入口是 actor-bearing override 经外部 metadata reader 间接创建。

现行策略明确采用 **existing-only**：EVH 不再通过这些协议自动创建新 Person。
须先有可精确查询的原生/人工 Person 才能复用；缺失对象持久 HOLD。
这会减少自动人物补充能力，尤其是 Douban-only 新人物，属于有意的安全收紧。
媒体尚未存在时延后人物处理；原 provider Episode 数据在内存中不被修改。

## 9. 失败策略

最终选择 B+C：持久 quarantine + 延后/后置人物处理；不采用自动删除补偿。
账本记录 operation_id、created_person_id（未知或未创建为 null）、target_person_id、
provider pair、source item、失败阶段和时间。先 reserve 后 mutation。
关联或映射不能确认、POST 结果不确定、进程中断、账本损坏时停止自动重试，
保留最后有效关联，不写回旧 People 快照，不 detach，不凭零引用 GET 删除。
当前没有一个会创建 transaction-owned 新 Person 的允许路径，不能将已有对象
或临时姓名匹配对象标成“本次新建”。

## 10. 并发

锁键使用 server namespace + provider pair 的 hash，保存于持久 volume，
POSIX flock 约束同 volume 的 EVH 进程；线程内可重入以复用写 gate。
双 worker 缺失身份测试只产生一次隔离记录、零创建请求。
这不是 Emby/第三方客户端的全局事务锁。没有宣称 GET 是服务器 CAS；
外部变化被观察到时拒绝，本轮不提供 destructive compensation。

## 11. 修改文件

沿用第一批：actor_utils.py、database/actor_db.py、handler/actor_sync.py、
tasks/maintenance.py、services/person_sync_safety.py、同步/PG 测试。

本批：core_processor.py、handler/emby.py、tasks/actors.py、tasks/media.py、
routes/media.py、services/person_write_safety.py、services/actor_enrichment_safety.py、
emby-actor-ui/src/components/MediaEditPage.vue、
tests/test_person_write_safety.py、tests/test_actor_enrichment_safety.py、
tests/test_douban_only_actor_ingest.py、scripts/verify_person_governance_lab.py、
本报告及设计/上游 delta 文档。

## 12. migration

**无 schema migration。** 没有改 database/connection.py 或 cleanup/delete schema。
账本位于 APP_DATA_DIR/person-governance，以 fsync + 原子 replace 持久化，
损坏/缺失证据不能放行。PostgreSQL 初始化只发生在新建临时 fixture 实例。

## 13–15. 自动测试

- 最终治理定向：87 个（同步 20、write safety 46、原 enrichment 15、Douban ingest 6）。
- 专用真实 PostgreSQL：5 个，连接级 TEMP 表验证身份冲突/不解绑/不合并。
- 完整 backend + PG：最终结果见本报告下方的验证记录。
- frontend contracts：68/68；Vite production build PASS。
- compileall / git diff --check PASS。
- 新安全断言覆盖：8+36、同名不同 ID、缺 provider、旧 mapping、manual identity drift、
  100→50 在 enrichment 前限额、固定目标集、8 不补 50、双 worker、持久失败/不可重放、
  已有不同姓名同身份复用、未知历史关联 HOLD、图片门控、缓存 scope 和姓名查询漂移。
- 原“临时姓名创建成功/失败后 detach”测试已改成相反的安全断言，
  没有将旧 bug 当作必须保持的功能。

## 16–18. Mac 真实集成

唯一 Emby：evh-lab-emby，4.10.1.0。新建专用本地样本库：
EVH Person Governance Fixture；Series 1139、Episode 1141。
复用已有精确人物创建样本关系，准备样本前后 Person 都为 124。
使用真实 Emby HTTP、完整 EVH Person pipeline、文件写入和专用 PostgreSQL；
TMDb 是受控 8+36 fixture，未访问公网 TMDb/Douban，图片备份不在此测试范围。

| 观测 | 第一次 | 第二次 |
|---|---:|---:|
| Person 总数 | 124 | 124 |
| Series People / parent targets | 8 | 8 |
| Episode People | 44 | 44 |
| Episode-local guest | 36 | 36 |
| provider mappings | 8 | 8 |
| 编辑页精确冻结目标 | 8 | 8 |
| 新 orphan | 0 | 0 |
| 新 provider duplicate | 0 | 0 |
| DeletePerson / DELETE / NAS 请求 | 0 | 0 |

基线已经存在的 orphan 不清理；测试要求它们的 ID 集保持不变。
原质量评分因样本只有 8 人将其标为待复核；未改评分/ReviewList，
也没有为提高评分补足人物。pipeline 成功与人物数量/关系的幂等断言均通过。

脱敏证据：本地 person-governance-phase2-20261014/state/pipeline-results.json。
该文件和样本媒体位于仓库外，不带入产品源码。

## 19. diff 摘要

tracked diff：13 个文件，1039 additions / 916 deletions（包含第一批暂停修改）。
主要删除临时创建/解绑和姓名广播；新增统一身份 gate、批量 scope/cap、
actor-bearing JSON 写前检查及 manual intent。
新增文件单列于 git status，不把未跟踪文件误算成 tracked diff。
未改虚拟库路由、CollectionType、播放器、播放逻辑、更新器、Inventory、
MediaInfo、STRM 或删除安全合同。

## 20. 工作区与上游 delta

工作区有预期未提交修改，**不是 clean**。未 commit/push/tag/release。
正式 v7.2.38 虚拟库修复工作树与正在运行的 Mac EVH 保持不动；本分支
继续保留原停止点，未来合入 main 时须审计版本间 diff，不回退虚拟库修复。

引用已经接受的上游固定 SHA：
`3b7ea3df77cba2d1b0ffec844aabf356e762e69b`。
借鉴 Series 自身主演员范围、明确 provider whitelist 和早期限额的原则；
没有 cherry-pick。上游 helper 的存在不等于它已覆盖所有 mutation：
EVH 另加了实例映射/写前完整 provider 复核和持久 HOLD。
禁止移植 automatic merge、orphan cleanup、destructive reconcile、Person delete，
也未移植其数据库主键/清空 mapping 方案。
这些收紧已纳入本阶段；历史治理/删除仍是后续独立阶段。

## 最终验证记录

完整 backend + PG：737 个，736 PASS、1 SKIP、0 FAIL/ERROR。
跳过的是 `test_self_update_docker_integration` 的 opt-in 真实 Docker 用例；
本轮明确冻结更新器，不将它冒充执行通过。

治理定向 87/87、专用 PG 5/5、frontend 68/68、Vite PASS，
compileall / diff-check PASS，真实 Mac 两次 pipeline PASS。
源码和脱敏证据的当前 Mac lab 凭据扫描 PASS。
临时 PG 容器与 tmpfs 合成数据已清除；原 lab 数据库未修改。
Mac EVH 仍为正式 7.2.38、healthy，本治理分支未部署。
媒体 fixture 与脱敏报告保留，方便复核和重新执行集成测试。

**PERSON CREATION PREVENTION = PASS（上述 existing-only / quarantine 支持边界内）。**
这不是新 Person 自动创建能力或历史清理资格的批准；
不提供服务端 CAS / 全局第三方并发原子性的承诺。
尚未发布，也没有将该代码部署到 NAS。
