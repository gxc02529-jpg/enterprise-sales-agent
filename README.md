# 企业内部销售数据分析 Agent

这是一个可运行的工业化骨架，不是只展示 Prompt 的 Demo。它将自然语言问题路由到销售统计、关系图谱、销售文档检索和报表导出四类参数化工具，并在输出前强制检查来源。

当前默认使用确定性的 Mock 数据，因此无需先安装 PostgreSQL、Redis、Milvus 或 NebulaGraph 即可跑通完整链路。生产适配器和真实数据接入是后续里程碑。

## 已实现

- LangGraph 主状态机：记忆预召回 → 意图路由 → 专家子图 → 失败重试 → 结果融合 → 输出校验。
- 4 类独立子图：SQL 指标、图谱关系、文档 RAG、报表导出；不是自由通信的多 Agent。
- FastMCP 工具边界：只接受 Pydantic 业务参数，不接受原始 SQL 或 nGQL。
- FastAPI：JWT 身份上下文、同步接口、SSE 流式接口和结构化审计日志。
- 三层记忆接口：session/user/business；实现租户、用户、权限标签、状态、过期和 token 预算过滤。
- PostgreSQL M1 适配器：指标/维度 allow-list、位置参数、事务级 RLS 上下文、查询模板指纹。
- 可切换 PostgreSQL Checkpointer，支持进程重启后的会话断点续跑。
- PostgreSQL 用户/业务记忆召回；个人记忆候选和显式确认 API 已形成闭环。
- 可切换 PostgreSQL 审计 Sink；HTTP 请求与 MCP 工具调用使用摘要指纹留痕，避免日志保存原始敏感入参。
- 可插拔 OpenAI-compatible LLM Provider，兼容自定义地址、模型、超时、重试和 token 预算。
- 规则/LLM 双模式路由与融合；LLM 故障自动降级，响应返回累计 token usage。
- API、MCP 工具和 LLM 三层韧性保护：deadline、有限重试、独立熔断、并发舱壁与入口限流。
- Milvus 3.x 文档 RAG：销售文档切片、BGE-M3、Dense+BM25、RRF、BGE-Reranker、双重权限过滤和引用返回。
- 管理员文档摄取 API/MCP 工具；Embedding、Reranker 和 PyMilvus 采用惰性加载。
- 治理模型：个人记忆默认只生成 candidate；业务记忆激活必须带审核人。
- PostgreSQL 初始化表、RLS 示例、Docker Compose 核心栈与 `full` 基础设施 profile。
- 单元测试：路由、多工具融合、来源校验、鉴权 API。

## 快速启动（本机）

要求 Python 3.11+。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn sales_agent.api:app --reload
```

请求示例：

```powershell
$body = @{
  query = "统计季度销售额，并查找华东智造客户的关系和最近拜访纪要"
  session_id = "demo-session"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/v1/analyze `
  -Headers @{ Authorization = "Bearer dev-token" } `
  -ContentType "application/json" `
  -Body $body
```

接口文档：`http://localhost:8000/docs`。SSE 端点为 `POST /v1/analyze/stream`。

记忆确认流程：

```text
POST /v1/memories/candidates
POST /v1/memories/candidates/{memory_id}/confirm
```

第一步只创建 `candidate`，只有用户显式调用第二个接口后才变为 `active` 并参与召回。

LLM 管理接口仅限 `admin` 角色：

```text
GET  /v1/admin/llm/config   # 返回脱敏后的生效配置
POST /v1/admin/llm/probe    # 发起一次最小模型连通性探测
GET  /v1/admin/resilience   # 查看 API、工具、LLM 韧性状态
POST /v1/admin/documents/ingest # 摄取已解析的销售文档
```

本地可以使用 `Authorization: Bearer dev-admin-token` 调试管理接口。生产环境必须关闭开发 token。

## Docker Compose

核心服务（API、MCP、PostgreSQL、Redis）：

```powershell
docker compose up --build
```

连同 Milvus、MinIO、etcd、NebulaGraph：

```powershell
docker compose --profile full up --build
```

要让 Agent 真正经 MCP 调用工具，将 `.env` 中 `TOOL_BACKEND=mcp`。默认 `mock` 让 API 单进程即可调试。

启用当前 M1 PostgreSQL 路径：

```dotenv
TOOL_BACKEND=mcp
DATA_BACKEND=postgres
CHECKPOINTER_BACKEND=postgres
MEMORY_BACKEND=postgres
AUDIT_BACKEND=postgres
LANGGRAPH_STRICT_MSGPACK=true
```

其中 `DATA_BACKEND=postgres` 将销售指标工具切到真实数据库；设置 `RAG_BACKEND=milvus` 可启用真实文档检索。Graph 和 Export 仍保留显式开发适配器。生产环境会拒绝 Mock 数据后端、Mock RAG、内存 Checkpointer 或内存长期记忆配置。

导入幂等演示数据后即可验证 PostgreSQL 指标链路：

```powershell
Get-Content .\examples\demo_seed.sql |
  docker compose exec -T postgres psql -U sales_agent -d sales_agent
```

## LLM 配置

默认 `LLM_REASONING_BACKEND=rules`，没有模型密钥也能完整运行。接入 OpenAI-compatible 服务时：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_API_STYLE=chat_completions
LLM_REASONING_BACKEND=llm
LLM_BASE_URL=https://your-llm-gateway.example.com/v1
LLM_API_KEY=由密钥管理系统注入
LLM_MODEL=your-default-model
LLM_ROUTER_MODEL=your-fast-model
LLM_SYNTHESIS_MODEL=your-quality-model
LLM_TIMEOUT_SECONDS=60
LLM_MAX_OUTPUT_TOKENS=1200
LLM_MAX_RETRIES=2
LLM_BUDGET_TOKENS=12000
```

Router 和 synthesis 模型留空时继承 `LLM_MODEL`。配置 API 永远不会返回 API Key；当前配置来源是环境变量或密钥管理系统，修改后重启服务生效。Provider 扩展点位于 `src/sales_agent/llm/provider.py`，完整契约见 [docs/llm-integration.md](docs/llm-integration.md)。

## RAG 与韧性配置

真实 RAG 需要安装 `.[infra,rag]` 并设置：

```dotenv
RAG_BACKEND=milvus
MILVUS_URI=http://localhost:19530
RAG_COLLECTION_NAME=sales_document_chunks
RAG_EMBEDDING_DEVICE=cpu
RAG_RETRIEVAL_TOP_K=20
RAG_RERANK_TOP_K=5
```

详细数据模型、权限过滤和摄取流程见 [docs/rag-pipeline.md](docs/rag-pipeline.md)。超时、熔断、限流和降级语义见 [docs/resilience.md](docs/resilience.md)。

## 目录

```text
src/sales_agent/
  agent/            LangGraph 状态、路由、专家子图、校验子图
  rag/              切片、Embedding、Reranker、Milvus 混合检索
  tools/            工具协议、FastMCP 客户端、Mock 适配器
  api.py            FastAPI / SSE
  mcp_server.py     参数化 FastMCP 工具服务
  memory.py         三层记忆召回与候选写入边界
  security.py       JWT 到 Principal/权限标签
deploy/postgres/    业务治理表、审计表、RLS 示例
docs/               架构、复用调研和迭代路线
tests/              最小回归测试
```

## 安全边界

- HTTP JWT 中的 `sub`、`tenant_id`、`roles`、`scope_tags` 会转换为不可缺省的 `Principal`。
- 每次工具调用都携带身份与 `request_id`；数据适配器必须再次做行级过滤，不能只依赖 API 层。
- LLM 永远不能提交可执行 SQL/nGQL。工具参数应映射到服务端 allow-list 查询模板。
- `dev-token` 仅供本地使用；生产必须设置 `ALLOW_DEV_TOKEN=false`，轮换 JWT/MCP 密钥并使用 TLS。
- PostgreSQL checkpointer 上线时应启用严格 msgpack 白名单并执行官方 `setup()` 初始化。
- `/health` 是进程存活探针；`/ready` 会检查 MCP 必需工具是否完整可用。

## GitHub 调研结论

截至 2026-09-22，没有发现一个仓库完整覆盖本项目的组合。建议“借组件，不整库 fork”：

| 项目 | 可复用部分 | 不直接采用的原因 |
|---|---|---|
| [agentic-rag-postgres-mcp](https://github.com/jayanthlocam/agentic-rag-postgres-mcp) | FastAPI、LangGraph、MCP、JWT、多租户 RAG、引用与评测 | pgvector 路线，无 NebulaGraph、Milvus 和受控三层记忆 |
| [langgraph-agent-memory](https://github.com/Ofekirsh/langgraph-agent-memory) | Redis 长期记忆、去重、抽取、MCP 调用流程 | 示例业务较窄，且缺少企业审核/只读全局记忆治理 |
| [confidentialmind-mcp-agent](https://github.com/ConfidentialMind/confidentialmind-mcp-agent) | 只读 PostgreSQL MCP、RAG MCP、结构化日志 | 工具仍偏通用数据库访问，不符合本项目业务参数化红线 |
| [langhost](https://github.com/langhost/langhost) | 自托管 LangGraph 服务、PostgreSQL/Redis 持久化 | 更适合作为后续运行时选项，不包含销售业务与数据治理 |
| [Sage](https://github.com/Krish-Parekh/Sage) | 检索前规划、两阶段 rerank、评测、鉴权与可观测 | Qdrant/Supabase 技术路线，只有文档 RAG |
| [Multi-Agent-Orchestration](https://github.com/Theepankumargandhi/Multi-Agent-Orchestration) | Router、RAG、知识图谱、checkpointer、CI/CD 组织方式 | 12 Agent 架构过重，与你“不滥用 A2A”的约束相反 |
| [LangGraph 官方 checkpoint-postgres](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres) | 生产 Checkpointer 的标准实现 | 是基础组件，不是完整业务项目 |
| [FastMCP](https://github.com/PrefectHQ/fastmcp) | MCP 服务、客户端、HTTP 鉴权和测试模式 | 是框架，需要自行实现权限、审计和业务工具 |

详细取舍见 [docs/reuse-research.md](docs/reuse-research.md)。

## 下一步

推荐按 [docs/roadmap.md](docs/roadmap.md) 的顺序推进：下一阶段补 PDF/Word/Excel 解析 Worker、Redis 分布式限流和 RAG 评测集；随后接 NebulaGraph CDC 与审核后台。
