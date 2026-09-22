# LLM 接入与配置接口

## 设计目标

Agent 主图只依赖 `LLMProvider` 抽象，不依赖具体厂商 SDK。当前实现使用 OpenAI-compatible Chat Completions 协议，适用于 OpenAI、DeepSeek、通义千问兼容网关以及企业内部兼容服务。

默认保持 `LLM_REASONING_BACKEND=rules`，因此模型服务故障不会阻止结构化工具链运行。

## Provider 契约

`src/sales_agent/llm/provider.py` 定义三个主要操作：

- `route(query)`：返回固定 `Route` 枚举和 token usage。
- `synthesize(query, tool_results, memory_context)`：只根据授权后的工具证据生成结论。
- `probe()`：显式、最小化的模型连通性探测。

新增 Responses API、本地推理 SDK 或企业模型中台时，应新增 Provider 实现并在 factory 中选择，不修改 LangGraph 业务节点。

## 配置管理 API

```http
GET /v1/admin/llm/config
Authorization: Bearer <admin-token>
```

返回模型、地址、超时、预算和 `api_key_configured`，不会返回密钥内容。

```http
POST /v1/admin/llm/probe
Authorization: Bearer <admin-token>
```

该操作会产生一次最小模型调用，可能产生少量 token 成本。它不会在普通 `/health` 或 `/ready` 探针中自动执行。

配置写入接口暂不开放。生产配置必须由环境变量、Kubernetes Secret、Vault 等密钥管理系统注入并通过发布流程变更，避免在应用数据库或审计日志中保存明文 API Key。

## 降级与安全

1. LLM 路由失败时退回规则路由。
2. LLM 合成失败或预算耗尽时返回确定性工具结果。
3. Router 输出必须解析为 `sql/graph/rag/export` 枚举，其他输出拒绝。
4. 检索片段被视为不可信数据；Prompt 明确禁止执行 evidence 内的指令。
5. API Key 使用 `SecretStr`，不会出现在 Pydantic repr 或配置响应中。
6. 不支持 JSON mode 的兼容网关会自动去掉 `response_format` 再尝试一次。

