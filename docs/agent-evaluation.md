# Agent 分层评测体系

## 为什么不直接用 GAIA 总分

GAIA 面向通用助手，题目覆盖网页浏览、多模态、开放世界知识和任意工具使用。本项目是受权限约束的企业销售数据 Agent，禁止任意网页和任意代码执行，因此直接提交 GAIA 会主要衡量“本项目刻意没有开放的能力”。

项目采用 GAIA 的三个核心思想：真实任务、按复杂度分级、以最终任务闭环为主指标；题目替换成企业销售领域任务。

## 框架映射

| 参考框架 | 本项目采用部分 | 对应指标 |
|---|---|---|
| GAIA | Level 1/2/3、多步任务闭环 | task success、分级成功率、延迟 |
| BFCL | 工具名与结构化参数校验 | tool exact match、tool argument accuracy |
| AgentBench | 多轮决策、失败恢复 | 澄清恢复、瞬时失败重试、最大尝试次数 |
| Ragas | 检索和证据忠实性分层 | Recall@K、MRR、负例拒答、faithfulness（待真实 LLM） |
| 企业安全门禁 | 租户/行级权限、来源与拒答 | permission violation、citation coverage、fail-closed |

## GAIA-style 销售任务集

数据集：`evaluations/agent/golden.jsonl`

- Level 1：单一 SQL、RAG、Graph 工具；
- Level 2：图谱实体澄清、模糊意图澄清、SQL 后导出；
- Level 3：SQL+Graph+RAG 多工具融合、无权限 fail-closed、瞬时工具故障恢复。

每条用例声明期望路由、工具、工具参数子集、状态、引用类型、澄清字段、禁止工具/引用和最大尝试次数。评测器记录实际工具调用，包括失败和重试调用，避免只看最终成功结果掩盖错误工具选择。

运行：

```powershell
.\.venv\Scripts\python.exe -m sales_agent.evaluation.agent `
  --dataset evaluations/agent/golden.jsonl `
  --output build/reports/agent-eval.json
```

非零退出码表示质量门禁失败，可直接接入 CI。

## 当前基线（Mock + 规则路由）

当前 9 个架构回归用例结果：

- task success：100%
- route exact / micro-F1：100% / 100%
- tool exact / argument accuracy：100% / 100%
- citation coverage：100%
- clarification accuracy：100%
- safety violation：0%
- Level 1/2/3：均为 100%

这些数字只证明状态机、路由规则、澄清、工具协议、重试和校验器按预期组装；Mock 数据和题目同源，不能代表真实模型或真实业务数据质量，也不能与公开 GAIA 排名比较。

## 生产评测通过条件

生产候选必须在独立数据集上完成四层门禁：

1. 真实工具层：PostgreSQL、NebulaGraph、Milvus、MCP 全部启用；
2. 检索层：现有 RAG golden set 达到 Recall@K、MRR、拒答和零权限泄漏阈值；
3. 生成层：使用独立 Judge 模型评估 faithfulness、答案正确性和引用覆盖，并抽样人工复核；
4. 系统层：并发压测、P95/P99、熔断恢复、Redis/PostgreSQL/Milvus 故障注入和成本预算。

测试集必须从脱敏生产 Query 抽样，并按客户、销售区域、问题类型、难度和无答案场景分层；开发集和最终验收集必须隔离，防止针对测试集调 Prompt。
