# API 说明

所有业务接口使用 `Authorization: Bearer <本地生成的 token>`。身份文件将 token 映射到 tenant、subject、roles；`X-Tenant` / `X-Subject` 不影响身份，JSON 未知字段会被拒绝。所有响应使用 `Cache-Control: no-store`。

默认身份：acme/writer（SOURCE_WRITER）、acme/alice（READER、PRODUCER）、acme/bob（READER）、beta/alice（READER、PRODUCER）。不在文档中提供可用 token；`scripts/demo.py` 从本地配置读取。

## 接口

| 方法与路径 | 角色 | 成功响应 |
| --- | --- | --- |
| POST `/v1/source-events` | SOURCE_WRITER | 200 来源事件结果 |
| POST `/v1/contexts/source` | READER 或 PRODUCER | 201 上下文元数据 |
| POST `/v1/contexts/derived` | PRODUCER | 201 上下文元数据 |
| POST `/v1/contexts/assemble` | READER 或 PRODUCER | 200 决策及正文；拒绝使用对应状态码 |
| GET `/v1/receipts/{id}` | 记录所属用户，READER 或 PRODUCER | 200 无正文决策记录 |
| GET `/health/live` | 无需认证 | 200 UP |
| GET `/health/ready` | 无需认证 | 200 UP；数据库或迁移不可用为 503 |

创建 SOURCE 请求：`{"source_id":"pricing-policy","ttl_seconds":300}`。

创建 DERIVED 请求：`{"content":"合成摘要","parent_ids":["父项 UUID"],"ttl_seconds":300}`。响应只返回 `id`、`kind`、`expires_at`、`depth`、`sources`；不能经元数据接口取出正文。

取用请求：`{"context_ids":["条目 UUID"]}`。成功响应结构：

```json
{
  "status": 200,
  "code": "ALLOWED",
  "receipt": {
    "id": "决策 UUID",
    "checked_at": "2026-09-29T10:00:00Z",
    "decision": "ALLOWED",
    "context_ids": ["条目 UUID"],
    "sources": [{"source_id":"pricing-policy","content_version":1,"auth_epoch":1}],
    "reasons": []
  },
  "items": [{"id":"条目 UUID","content":"合成正文"}]
}
```

上例 UUID 为说明占位符。策略拒绝仍返回 `receipt`，`items` 必定为空。结构不合法、身份失败、非自有资源或数据库故障只返回固定 `{"code":"..."}`，不会回显用户输入。格式失败和 404 不写入决策表，数据库不可用时也无法保证持久化拒绝。

## 完整来源快照

```json
{
  "source_id": "pricing-policy",
  "sequence": 1,
  "content": "仅用于演示的合成报价",
  "readers": ["alice", "bob"],
  "state": "ACTIVE",
  "fresh_until": "替换为当前时间后不超过五分钟的 ISO-8601 时间"
}
```

`sequence` 为正整数，允许跳号。readers 按集合排序参与 hash，不允许重复。`fresh_until` 规范化到微秒；已过期快照可以写入，它将导致后续取用失败。同序号同快照返回原结果，过期后重放也不续期。同序号异参 409；未知旧序号返回 `IGNORED_STALE` 且不改变当前状态。

正文变化递增 `content_version`；授权名单或状态变化递增 `auth_epoch`；仅续期不递增它们。源级 epoch 是保守粒度：其他用户的权限改变，也会让仍有权限用户的旧条目失效。

删除快照必须 `state=DELETED`、`content=""`、`readers=[]`。删除是终态；重新导入需新 source_id。

## 失败语义与限制

| HTTP | 代表代码 | 含义 |
| --- | --- | --- |
| 400 | INVALID_JSON / INVALID_ITEMS / INVALID_TTL / TOO_MANY_SOURCES / MAX_DEPTH_EXCEEDED | 输入、来源集合或深度不合法 |
| 401 | UNAUTHENTICATED | 缺失或无效身份 |
| 403 | FORBIDDEN / SOURCE_ACCESS_DENIED | 角色或当前来源权限不足 |
| 404 | NOT_FOUND | 不存在、跨租户或非本人条目/决策，统一响应 |
| 409 | EVENT_CONFLICT / CONTEXT_STALE | 重复事件冲突或绑定的版本/epoch 过时 |
| 410 | SOURCE_DELETED / CONTEXT_EXPIRED | 来源删除或条目期限结束 |
| 413 | REQUEST_TOO_LARGE / CONTENT_TOO_LARGE / BATCH_TOO_LARGE | 请求或正文超过限制 |
| 503 | SOURCE_UNVERIFIED / DATABASE_UNAVAILABLE / NOT_READY | 来源新鲜度不足、主库不可用或尚未就绪 |

优先检查资源归属；自有批次中权限不足优先于删除、过期、版本变化和新鲜度不足。每项只记录一个最高优先级原因。全批次来源/正文超限会拒绝整个批次。

限制：请求体 300 KiB；单项正文 UTF-8 64 KiB；一次 1–16 项、总正文 256 KiB；最多 32 个不同叶子来源；派生深度 4；父项必须同用户且当前有效；每源 readers 最多 100；条目 TTL 1–900 秒（默认 900）；源新鲜度最多未来 300 秒。派生到期时间取请求期限与全部父项期限的最小值。

期限按取得 guard 锁后采样的数据库时间判定，`checked_at >= expires_at/fresh_until` 即过期。网络传输时间不在这项保证内。
