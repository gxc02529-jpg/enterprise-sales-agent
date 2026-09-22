# 实施路线

## M0：当前骨架

- 可运行 API、SSE、LangGraph/FastMCP 边界和 Mock 端到端链路。
- 安全不变量、数据模型、RLS 示例、部署拓扑和测试基线。

## M1：真实 SQL 指标闭环

1. 固化 CRM 数据字典、指标口径、时间口径和权限矩阵。
2. 实现 PostgreSQL 只读账号、参数化查询模板和事务级 RLS 上下文。
3. 引入 AsyncPostgresSaver 和 Redis session cache。
4. 建立 30～50 条销售指标 golden set，验证数值准确率和权限负例。

验收：Top 20 高频统计问题结果与 BI 报表一致；越权测试 100% 拒绝；结果包含查询快照 ID。

当前进度：参数化 PostgreSQL 指标适配器、事务级 RLS、PostgreSQL Checkpointer、持久化记忆召回、个人记忆确认 API 及 PostgreSQL 审计 Sink 已完成。待接入企业真实 CRM schema、真实数据和权限负例集后完成业务验收。

## M2：文档 RAG

1. 文档解析、去重、切片、权限标签和摄取状态机。
2. Milvus dense（BGE-M3）+ sparse/BM25，通过 RRF 融合。
3. BGE reranker 取 Top-N；低分拒答并返回命中文档。
4. 将检索和生成指标分开评测。

验收：业务测试集 Recall@K、MRR、faithfulness 达到事先约定阈值；无跨用户文档泄露。

当前进度：已完成纯文本摄取合同、确定性切片、BGE-M3/BGE-Reranker 惰性适配、Milvus 3.x Dense+BM25+RRF 混合检索、MCP 摄取/查询边界、服务端过滤与客户端权限二次校验、引用生成。待完成多格式解析 Worker、缓存、检索策略路由和业务评测集。

## M3：知识图谱

1. 建立实体 ID、别名表、关系 schema 和 CDC outbox。
2. 结构化数据先入图；文档抽取只写待审 change request。
3. 实现审核、版本、快照和回滚后台。
4. MCP Graph 仅暴露 allow-list traversal 模板，限制 4 hops、结果数和执行时长。

验收：关系准确率、实体链接准确率、孤立点率和版本回滚演练通过。

## M4：记忆与生产治理

1. Redis session TTL、摘要压缩、大结果对象存储引用。
2. 用户记忆确认 UI、business memory 审批流和全量审计。
3. token/时延预算、缓存、熔断、人工介入队列。API/工具/LLM 三层超时、重试、熔断、舱壁和单实例入口限流已完成；待接入 Redis 分布式限流与告警面板。
4. 指标面板、离线评测、灰度发布、备份恢复和运维手册。

验收：断点续跑、故障注入、数据恢复、密钥轮换和模型降级演练全部通过。
