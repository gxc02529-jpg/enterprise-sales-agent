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

当前接口接收已经抽取的纯文本。PDF、Word、表格和 OCR 解析属于下一阶段独立 Worker，不放在在线 API 进程中执行。

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

- PDF/Word/Excel/MinerU 解析 Worker 和摄取状态机；
- Redis 权限化查询缓存；
- Direct/SubQuery/Backtrack 检索策略路由；
- RAGAS、Recall@K、MRR 和权限负例测试集；
- 文档版本后台、删除/失效和人工审核页面。
