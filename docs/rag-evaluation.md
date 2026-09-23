# RAG 离线评测

## 目的

检索参数、Embedding、Reranker 或切片策略变更必须经过同一组业务问题回归，不能只观察单次问答。当前评测器将“是否找对证据”和“是否允许生成答案”分开计算，并把权限泄漏作为硬门禁。

## 数据集

- `evaluations/rag/documents.jsonl`：可重复摄取的受控文档，覆盖华东、华南和第二租户；
- `evaluations/rag/golden.jsonl`：正常命中、跨区域管理员、跨区域销售拒绝、跨租户禁止和无关问题拒答；
- `evaluations/rag/mock-smoke.jsonl`：仅验证评测程序可以运行，不代表真实检索质量。

每个查询用例包含执行检索所需的 `Principal`，以及：

- `expected_document_ids`：应该出现在 Top-K 中的文档；
- `forbidden_document_ids`：无论相关性多高都不允许返回的文档；
- `customer_ids`、`document_types`：可选业务过滤条件；
- `top_k`：本用例的评测深度。

评测报告只保存 case ID、文档 ID、分数和聚合指标，不保存命中文档正文。

## 指标

- `Recall@K`：预期文档在 Top-K 中被召回的比例；
- `MRR@K`：第一个相关文档排名的倒数；
- `HitRate@K`：至少命中一个相关文档的用例比例；
- `negative_rejection_rate`：无答案/无权限用例返回空结果的比例；
- `permission_violation_rate`：返回禁用文档的用例比例，默认阈值必须为 0。

## 真实 Milvus 评测

先启动 full profile，并准备模型依赖：

```powershell
docker compose --profile full up -d
.\.venv\Scripts\python.exe -m pip install -e ".[infra,rag]"
$env:RAG_BACKEND = "milvus"
$env:MILVUS_URI = "http://localhost:19530"
```

首次运行可摄取受控 fixture：

```powershell
.\.venv\Scripts\python.exe -m sales_agent.evaluation.rag `
  --dataset evaluations/rag/golden.jsonl `
  --fixtures evaluations/rag/documents.jsonl `
  --ingest-fixtures `
  --output build/reports/rag-eval.json
```

后续回归不需要重复摄取，可移除 `--fixtures --ingest-fixtures`。默认门禁为 Recall@K ≥ 0.8、MRR@K ≥ 0.7、负例拒答率 100%、权限泄漏率 0%。命令返回非零退出码表示门禁失败，可直接接入 CI。

## 阈值校准

`RAG_MIN_SCORE` 是 BGE Reranker 归一化分数的拒答阈值，默认 0.35。关闭 Reranker 时使用独立的 `RAG_MIN_RETRIEVAL_SCORE`，因为 RRF/BM25 分数和 Reranker 分数不在同一尺度。阈值调整必须同时观察正例 Recall 与负例拒答率，不能只追求召回率。

完整答案的 faithfulness/引用覆盖率属于生成评测，后续在检索门禁稳定后加入，避免把“检索没找对”和“模型没有忠实使用证据”混成一个指标。
