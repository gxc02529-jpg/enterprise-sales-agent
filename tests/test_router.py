from sales_agent.agent.router import route_query
from sales_agent.contracts import Route


def test_multi_route_query() -> None:
    routes = route_query("统计季度销售额，并查看客户关系和最近拜访纪要")
    assert routes == [Route.SQL, Route.GRAPH, Route.RAG]


def test_export_alone_gets_data_route() -> None:
    assert route_query("请导出 Excel") == [Route.SQL, Route.EXPORT]
