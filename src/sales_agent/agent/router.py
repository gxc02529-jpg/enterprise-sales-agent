from __future__ import annotations

from sales_agent.contracts import Route

SQL_TERMS = {
    "销售额",
    "金额",
    "营收",
    "同比",
    "环比",
    "统计",
    "汇总",
    "平均",
    "排名",
    "多少",
    "趋势",
    "季度",
    "月份",
    "合同数",
    "revenue",
    "count",
    "sum",
}
GRAPH_TERMS = {
    "关系",
    "关联",
    "负责",
    "签订",
    "包含",
    "属于",
    "对标",
    "路径",
    "多跳",
    "上下游",
    "客户网络",
    "谁负责",
    "relationship",
    "graph",
}
RAG_TERMS = {
    "纪要",
    "文档",
    "拜访",
    "说过",
    "提到",
    "需求",
    "反馈",
    "竞品方案",
    "原文",
    "风险",
    "预算",
    "交付",
    "document",
    "note",
}
EXPORT_TERMS = {"导出", "报表", "下载", "excel", "xlsx", "export"}


def route_query(query: str) -> list[Route]:
    """High precision baseline router; replace or augment with structured LLM routing later."""

    normalized = query.lower()
    routes: list[Route] = []
    groups = (
        (Route.SQL, SQL_TERMS),
        (Route.GRAPH, GRAPH_TERMS),
        (Route.RAG, RAG_TERMS),
        (Route.EXPORT, EXPORT_TERMS),
    )
    for route, terms in groups:
        if any(term in normalized for term in terms):
            routes.append(route)
    if not routes:
        routes.append(Route.RAG)
    if routes == [Route.EXPORT]:
        routes.insert(0, Route.SQL)
    return routes
