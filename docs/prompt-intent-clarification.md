# Prompt、意图路由与澄清治理

## 1. Prompt 注册中心

运行时 Prompt 不再写在 Python 代码中，而由 `config/prompts.json` 管理。每条记录包含：

- `key`：稳定的业务用途标识；
- `version`：不可复用的版本号；
- `locale`：语言/地区，例如 `zh-CN`、`en-US`；
- `status`：同一个 `key + locale` 只能有一个 `active`；
- `variables`：显式变量白名单，防止漏传或误传；
- `content`：Prompt 正文。

服务按文件 mtime 热重载。发布新版本时新增记录、将旧版本改为 `inactive`，再原子替换配置文件；回滚则反向切换状态，不改业务代码。启动和热重载都会校验重复版本、多个 active、变量缺失。管理员可通过 `GET /v1/admin/prompts` 查看当前目录和版本。

生产建议由 GitOps/配置中心分发该文件，并保留评测结果、审批人和变更单号。当前实现没有开放“在线改 Prompt”写接口，避免多实例本地文件写入产生分叉；后续若接入 PostgreSQL/配置中心，应保持同一 `PromptRegistry` 契约。

## 2. 真正的澄清模式

澄清不是生成一句“请补充信息”后结束，而是 LangGraph 的持久化暂停/恢复：

1. 意图路由后进入 `clarification_gate`；
2. 路由置信度不足，或图谱查询缺少起始实体时，返回结构化 `clarification`；
3. `await_clarification` 调用 `interrupt()`，checkpoint 保存节点、状态和原问题；
4. 客户端以同一 `session_id` 调用 `POST /v1/analyze/clarify`；
5. API 使用 `Command(resume=answers)` 从中断点继续，随后才允许进入工具子图。

暂停态 `status=needs_clarification`，且 `tool_results=[]`，因此不会在信息不完整时误查数据。生产使用 PostgreSQL Checkpointer 后，进程重启仍可恢复；内存 Checkpointer 仅适合开发。

请求示例：

```json
POST /v1/analyze
{"query":"查看客户关系","session_id":"s-100"}
```

```json
POST /v1/analyze/clarify
{
  "session_id":"s-100",
  "answers":{"entity_name":"华东智造公司"}
}
```

## 3. Laya 意图路由

Laya 是可选的 System-1 路由后端，不参与答案生成。安装与启用：

```powershell
python -m pip install -e ".[laya]"
```

```dotenv
INTENT_ROUTER_BACKEND=laya
INTENT_CONFIDENCE_THRESHOLD=0.65
LAYA_MODEL=auto
LAYA_DEVICE=cuda
LAYA_PRELOAD=true
```

适配器在一次 forward 中用四个 `noul` 问题支持多标签路由。`auto` 让 Laya Router 为中文选择 multilingual checkpoint。模型加载放在线程池并可预热，避免阻塞 FastAPI event loop；异常会降级到规则路由，低置信结果进入澄清流程。

上线前必须用本项目真实 Query 建立意图测试集，分别评估每类 precision/recall、混淆矩阵、拒识率与 P95 延迟，再校准阈值。Laya 官方也说明 multilingual 概率默认未做温度拟合、基础模型零样本能力有限，因此不能直接把官方延迟或置信度当成本项目生产结论。
