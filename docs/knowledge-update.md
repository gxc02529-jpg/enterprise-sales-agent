# 高频知识库更新策略

## 当前已实现

- `content_hash` 幂等：正文和索引元数据都未变化时返回 `unchanged`，避免重复 Embedding 和重复写入；
- 版本化 chunk：每个命中保留 `document_id/version/content_hash/chunk_index`，引用可以定位具体版本；
- 普通内容更新采用“新 chunk 先 upsert、旧 content_hash 后删除”，避免搜索出现空窗；
- ACL 变化采用 fail-closed：先删除旧权限版本，再写新版本，宁可短暂查不到，也不允许旧权限短暂泄漏；
- `DELETE /v1/admin/documents/{document_id}` 和 MCP `delete_document` 支持文档下线；删除严格带 `tenant_id + document_id` 条件；
- 摄取仍经过持久化任务、重试、死信与审计链路，不能由 Agent 直接操作 Milvus。
- PostgreSQL 使用 `knowledge_document` 保存当前/待处理版本，
  `knowledge_document_version` 保存不可变版本账本；Worker 在写 Milvus 前锁定文档 head。
- 同版本同内容直接幂等完成；同版本不同内容判定为冲突并进入失败队列；
  更旧的 `source_updated_at` 事件标记为 `superseded`，不会调用 Embedding/Milvus。
- 激活版本和写入 `knowledge.document.activated` outbox 事件与摄取任务完成处于同一 PostgreSQL 事务。
- 同文档已有 generation 正在索引时，新事件延后重新入队且不消耗失败次数，避免正常锁竞争误入死信。

## 推荐的更新入口

上游 CRM、文档平台不要直接写向量库。统一发出带以下字段的变更事件：

```text
event_id, tenant_id, document_id, version, operation,
content_uri/content, content_hash, permission_tags, occurred_at
```

`event_id` 用于消费幂等，`document_id + version` 用于顺序控制；删除也必须形成 tombstone 事件，不能只删除源文件。

## 已落地的 PostgreSQL 协调流程

Milvus 本身不能和 PostgreSQL 做跨库事务，因此当前采用可恢复的 saga：

1. Redis Stream 至少一次投递摄取任务；
2. Worker 在 PostgreSQL 锁定 `tenant_id + document_id` head，并预占 pending version；
3. 重复和乱序事件在调用向量模型前被拦截；
4. Worker 写入 Milvus；失败保留任务并按策略重试/死信；
5. 成功后在一个 PostgreSQL 事务内切换 active version、完成任务并写 outbox。

`source_updated_at` 应取上游文档平台的更新时间；未提供时使用任务创建时间。版本字符串只作为不可变标识，不使用字符串大小判断先后，避免 `"10" < "2"` 这类错误。

## 仍需补齐

1. outbox relay：发布 cache invalidation 并将事件标记为 `published`；
2. Milvus 查询按 PostgreSQL active generation 过滤，实现严格原子可见切换；
3. 保留旧版本原文/对象存储地址，提供真正的一键回滚重建；
4. 针对同文档热点更新增加按 document key 的队列分区，减少 `busy` 重试。

当前已经解决版本冲突、重复投递、乱序旧事件和 Worker 中断恢复；“查询侧 active generation 原子过滤”和 outbox relay 仍是下一里程碑，不能宣称已经具备跨库强一致。
