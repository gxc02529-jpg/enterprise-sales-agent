# GitHub 复用调研（2026-09-22）

## 结论

没有找到可直接作为本项目基座、同时满足以下条件的公开仓库：LangGraph 子图、FastMCP 业务参数工具、PostgreSQL 聚合、NebulaGraph 多跳、Milvus BGE-M3/BM25/rerank、受控三层记忆、行级权限、审计与人工审核。

因此不建议 fork 单一项目。更稳妥的策略是：使用 LangGraph/FastMCP 官方包作为内核，参考若干仓库的局部模式，把企业权限与治理留在自有代码中。

## 候选项目

### agentic-rag-postgres-mcp

- 地址：https://github.com/jayanthlocam/agentic-rag-postgres-mcp
- 值得复用：文档上传摄取、带引用问答、FastAPI/REST/MCP 三种入口、JWT 多租户隔离、评测基线。
- 差距：向量存储为 pgvector，缺少图库、结构化销售统计路由和受控业务记忆。

### langgraph-agent-memory

- 地址：https://github.com/Ofekirsh/langgraph-agent-memory
- 值得复用：Redis 向量记忆、去重、记忆抽取、分块和 LangGraph/MCP 串联。
- 差距：长期记忆会自动提取和保存，不满足“个人确认、全局审核”的企业红线。

### confidentialmind-mcp-agent

- 地址：https://github.com/ConfidentialMind/confidentialmind-mcp-agent
- 值得复用：PostgreSQL/RAG MCP 分服、HTTP/stdio 双模式、结构化可观测性。
- 差距：通用 `execute_sql` 风格不适合作为生产销售工具。即使只读也会扩大越权、资源耗尽和语义错误面。

### langhost

- 地址：https://github.com/langhost/langhost
- 值得复用：LangGraph Agent Server 的开源自托管、PostgreSQL/Redis 持久化与生态协议兼容。
- 差距：属于运行时，不负责业务数据、工具治理和 RAG/图谱建设。可在第二阶段做运行平台 PoC。

### Sage

- 地址：https://github.com/Krish-Parekh/Sage
- 值得复用：先规划再检索、两阶段 rerank、输入 guard、检索/生成分开评测、JWT/JWKS。
- 差距：聚焦单一 RAG，基础设施为 Qdrant/Supabase。

### Multi-Agent-Orchestration

- 地址：https://github.com/Theepankumargandhi/Multi-Agent-Orchestration
- 值得复用：路由元数据、checkpointer fallback、混合检索参数和 CI/CD 目录组织。
- 差距：12 个 Agent 的 supervisor 架构对本场景过度设计，通信与评测面明显扩大。

## 官方组件

- LangGraph PostgreSQL checkpointer：https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres
- FastMCP：https://github.com/PrefectHQ/fastmcp
- Milvus：https://github.com/milvus-io/milvus
- NebulaGraph：https://github.com/vesoft-inc/nebula

优先直接依赖官方包，不复制其内部实现。特别是 checkpointer，应跟随官方安全修复并启用严格反序列化配置。

