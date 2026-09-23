# 销售文档 RAG

## 边界

Agent 只能调用 MCP `search_documents`，不能生成 Milvus filter、访问 Collection 或自行构造向量查询。文档摄取只开放给 `admin`/`knowledge_admin`，普通销售用户没有写入入口。

## 摄取链路

```text
已提取文本
  -> 规范化与确定性切片
  -> content_hash/chunk_id 去重标识
  -> BGE-M3 dense embedding
  -> tenant/owner/customer/permission metadata
  -> Milvus upsert
  -> BM25 Function 自动生成 sparse vector
```

同步接口接收已经抽取的纯文本。异步任务接口支持 TXT、Markdown、JSON、CSV，并为 PDF、DOCX、XLSX 提供可选解析器。解析和索引由 Worker 执行，不阻塞请求线程；开发环境使用有界内存队列，生产环境可切换为 PostgreSQL 任务状态 + Redis Stream 消费者组。扫描件 OCR/MinerU 仍属于后续独立 Worker。

### 持久化任务模式

`INGESTION_BACKEND=redis_stream` 时，上传正文、身份快照、权限标签和任务状态写入 PostgreSQL，Redis Stream 仅保存 `job_id`：

```text
HTTP upload
  -> PostgreSQL INSERT(status=queued, content=bytea)
  -> Redis Lua: 去重 + 容量检查 + XADD(job_id)
  -> XREADGROUP/XAUTOCLAIM
  -> PostgreSQL lease claim
  -> parse -> index -> completed
  -> XACK + XDEL + 释放容量
```

该模式提供 at-least-once 处理；消费者崩溃后由 `XAUTOCLAIM` 接管。数据库租约阻止同一任务被两个 Worker 同时占用，Milvus 摄取使用确定性 chunk ID，允许故障窗口内安全重放。成功后清除 PostgreSQL 中的原始文件正文；失败任务按配置保留正文，支持管理员人工重试。重试耗尽进入 `dead_letter`，死信 Stream 只记录任务 ID 和错误码，不复制文档正文。租户管理员只能查看本租户任务，Worker 通过独立 RLS 上下文处理跨租户队列。

运维接口：

- `GET /v1/admin/documents/jobs?status=failed`：按状态查看本租户任务；
- `GET /v1/admin/documents/jobs/{job_id}`：查看任务、尝试次数和错误码；
- `POST /v1/admin/documents/jobs/{job_id}/retry`：重新投递仍保留原文的失败或死信任务。

默认失败原文保留 7 天、终态任务记录保留 90 天。后台清理只运行固定参数 SQL，并在 `/v1/admin/resilience` 暴露累计清理数。

`POST /v1/admin/documents/ingest` 请求核心字段：

- `document_id`、`title`、`document_type`、`text`；
- `customer_ids`：文档关联客户；
- `permission_tags`：部门、区域或销售范围标签；
- `owner_user_id`：缺省时使用摄取人；
- `source_uri`、`version`：用于引用和版本追踪。

同一租户、同一 `document_id` 再次摄取时先删除旧 chunks 再写入新版本。Chunk ID 由租户、文档 ID、正文哈希和序号确定，同样输入得到同样 ID。

## 在线检索

```text
query
  -> BGE-M3 query embedding
  -> Dense COSINE ─┐
                   ├-> RRF -> BGE-Reranker -> Top-K -> citations
  -> Milvus BM25 ──┘
```

Milvus 两路 `AnnSearchRequest` 使用完全相同的权限过滤表达式。结果返回后，应用层再次检查租户、owner/scope、customer 和 document_type，防止数据库版本差异或错误配置造成越权结果泄露。

## Collection 设计

第一阶段使用一个 `sales_document_chunks` Collection，通过 `document_type` 表达业务分类，避免为每个租户创建 Collection 造成运维膨胀。主要字段包括：

- 身份：`tenant_id`、`owner_user_id`、`permission_tags`；
- 业务：`document_id`、`document_type`、`customer_ids`、`version`；
- 来源：`title`、`source_uri`、`chunk_index`、`content_hash`；
- 检索：`text`、`dense_vector`、`sparse_vector`。

## 启用方式

默认 `RAG_BACKEND=mock`，无需模型和 Milvus 即可开发。真实模式需要：

```bash
pip install -e ".[infra,rag]"
docker compose --profile full up -d
```

然后设置 `RAG_BACKEND=milvus`。BGE 模型采用惰性加载，第一次摄取/检索才加载；生产环境应提前下载模型到内网模型仓库并执行预热。

## 尚未完成

- 扫描件 OCR/MinerU 独立解析 Worker；
- 文档版本后台、主动删除/失效、人工审核和失败原文下载审批；
- Redis 权限化查询缓存；
- Direct/SubQuery/Backtrack 检索策略路由；
- 扩充真实业务 golden set，并补 RAGAS faithfulness/引用覆盖率生成评测；
