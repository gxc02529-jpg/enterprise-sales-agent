# 韧性与降级设计

## 三层保护

### API 入口层

- 以 `tenant_id:user_id` 为键执行 60 秒滑动窗口限流。
- 每个身份拥有独立并发配额，等待舱位超时返回 `503 API_SATURATED`。
- 超过频率返回 `429 RATE_LIMITED` 和 `Retry-After`。
- 整个分析请求有端到端 deadline；普通接口超时返回 504，SSE 返回 `failed` 事件。

当前实现是单进程内存限流，适合开发和单实例部署。多副本生产环境应把同一接口替换为 Redis Lua/网关限流，确保所有 API 实例共享配额。

### MCP 工具层

每个工具有独立的：

- 调用超时；
- 有界指数退避重试；
- 熔断状态机（closed/open/half-open）；
- 并发舱壁。

`sales_metrics`、`graph_relations`、`search_documents` 是只读调用，可以有限重试。`export_report` 和 `ingest_document` 可能产生写入，不自动重试，避免重复任务。

API 到 MCP、MCP 到数据适配器是两段独立故障域。SQL 熔断不会阻止 RAG 或图谱工具运行。管理员可以通过 `GET /v1/admin/resilience` 查看 API、工具和 LLM 的脱敏状态，也可以调用 MCP `resilience_status` 查看服务端工具状态。

### LLM 层

- HTTP 客户端连接/读取超时之外，再设置整个生成操作 deadline。
- 仅对网络错误、408、425、429 和 5xx 进行重试；参数错误和协议错误不重试。
- 熔断后 LangGraph 路由降级到规则路由，答案合成降级为确定性工具结果。
- token 预算耗尽时不再调用模型。

## 观测原则

失败事件只记录异常类型、耗时及输入输出摘要，不把客户原始问题、文档正文或凭据写入审计日志。HTTP 4xx/5xx 和 MCP 工具异常都会标记为 `failed`。
