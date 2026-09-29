# 架构与并发边界

[English overview](../README.md) | [中文概览](../README_ZH.md) | [API](api.md) | [验证](validation.md)

ContextFence 管理已经生成的上下文能否再次取用。两个相同 Java 服务连接同一 PostgreSQL 主库，各自拥有独立正文缓存。受信来源适配器提交完整快照；业务后端创建 SOURCE/DERIVED 条目，并通过 `assemble` 重新取正文。

![Read validation and source updates share a PostgreSQL lock boundary.](images/contextfence-overview.svg)

## 数据与身份

演示凭据在启动时随机生成，服务端将 token 映射为 tenant、subject 与角色。身份来自认证映射，不能由 JSON、`X-Tenant` 或 `X-Subject` 自报。条目属于创建用户；跨租户或非本人资源统一不可见。首版不支持跨用户共享。

六张表保留明确的状态和事务关系：

| 表 | 职责 |
| --- | --- |
| `tenant_guard` | 同租户读写事务的共享/排他行锁边界 |
| `source_state` | 当前正文、允许列表、状态、序号、content_version、auth_epoch、fresh_until |
| `source_events` | 完整快照规范化 hash 与已提交处理结果，用于重放及冲突判断 |
| `context_items` | 不可变条目、归属、正文/来源引用、父项、到期时间、深度与正文 hash |
| `context_sources` | 条目的叶子来源集合及绑定的 content_version/auth_epoch |
| `admission_receipts` | 不含正文及凭据的取用决策记录 |

资源键、约束与关联携带 tenant。来源允许列表是有界 subject 集合，不是完整 IAM 平台。

## 完整快照与两个版本

来源事件包含 source_id、正整数 sequence、完整正文、允许列表、状态与 fresh_until。更大的序号可跳号，因为事件包含完整状态；这一规则不能直接用于增量补丁。

- 同序号、同规范化 hash 返回原处理结果，不重写也不续期。
- 同序号异参返回 409 `EVENT_CONFLICT`。
- 未知旧序号记录 `IGNORED_STALE`，不改变当前状态。
- 正文改变递增 `content_version`；允许列表或 ACTIVE/DELETED 状态改变递增 `auth_epoch`。仅延长新鲜度不递增版本。
- DELETED 是终态，重新导入必须使用新 source_id。

正文版本回答“资料是否改变”，授权 epoch 回答“授权历史是否改变”。撤权再授权仍改变 epoch，所以旧上下文不会复活。源级 epoch 的粒度较保守：即使某位用户仍有权限，其他用户的允许列表变化也会使该源的旧上下文失效。

## 登记派生依赖

SOURCE 条目绑定当前来源版本与 epoch。DERIVED 条目由 PRODUCER 提交正文及 parent_ids；服务端合并父项的叶子来源集合，不接受客户端自行声称来源版本。父项必须已存在、同租户同用户且当前有效；同一来源的不同版本不能合并。

条目不可变且只能引用既有父项，关系不能成环。派生项继承父项最早到期时间，并受请求 TTL 上限约束。每次取用比较叶子来源绑定与当前状态，因此无需先异步遍历所有后代才阻断旧内容。来源声明由受信生产者负责，系统无法检测文本中未登记的语义依赖。

## 一次取用的事务

1. 认证后确定 tenant/subject，开始 READ COMMITTED 事务，对 `tenant_guard` 执行 `FOR SHARE`。
2. 取得锁后用新的 SQL 读取自有条目和当前来源。此顺序避免使用等锁之前的过时状态。
3. 以锁后采样的数据库 `clock_timestamp()` 检查条目期限与 source fresh_until，并核验权限、删除状态、content_version 与 auth_epoch。`checked_at >= deadline` 即失效。
4. 所有条目通过后才从数据库或不可变正文缓存复制有界正文。任一条目失败，整个批次 `items` 为空。
5. 保存无正文决策记录并提交，再向 HTTP 返回结果。策略拒绝作为决策值提交，避免异常回滚导致拒绝审计丢失。

来源更新对同一 guard 行持 `FOR UPDATE`，在事务内原子写状态、事件与处理结果，提交成功后才返回确认。guard 锁内没有外部网络或模型调用。

**承诺针对成功撤权确认后新发起的取用。** 先取得共享锁的重叠请求可以完成，其响应可能晚于撤权确认抵达。期限保证针对锁后判定时刻；已放行响应可能在到期后送达。服务无法撤回已经交付的内容。

## 缓存与故障

缓存键包含 tenant、item_id 与不可变正文 hash，只缓存正文，不缓存准入结果。每次取用都查询主库的当前授权与版本；无需依赖缓存广播及时到达。主库不可达时返回 503，不回退旧缓存。HTTP 响应使用 `Cache-Control: no-store`。

事件提交前进程终止不会留下半写；提交后确认丢失时，重送相同 sequence 和完整快照可恢复已保存结果。这是可重放事务结果，不是 HTTP exactly-once 承诺。数据库不可用时也不能保证持久化拒绝记录。

## 范围与成本

请求体最多 300 KiB；单项 UTF-8 正文 64 KiB；一次取用 1–16 项、总正文 256 KiB；叶子来源最多 32 个；派生深度最多 4；每源允许列表最多 100 个 subject。条目 TTL 为 1–900 秒，来源新鲜度最多未来 300 秒。

`fresh_until` 约束服务已知状态的年龄，不能发现尚未同步的上游变化。删除是后续访问阻断，不是物理擦除。单主库、租户级锁与逐项有界查询有利于解释一致性，也会影响更新延迟；本版本没有生产吞吐承诺。真实身份提供商、资料连接器和保留策略属于独立集成。
