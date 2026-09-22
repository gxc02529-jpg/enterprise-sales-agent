import pytest
from fastmcp import Client

from sales_agent.mcp_server import mcp


@pytest.mark.asyncio
async def test_mcp_business_contract() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        assert {
            "sales_metrics",
            "graph_relations",
            "search_documents",
            "export_report",
        }.issubset({tool.name for tool in tools})
        result = await client.call_tool(
            "sales_metrics",
            {
                "principal": {
                    "user_id": "sales-1",
                    "tenant_id": "tenant-1",
                    "roles": ["sales"],
                    "scope_tags": ["sales:sales-1"],
                },
                "params": {
                    "metrics": ["revenue"],
                    "dimensions": ["quarter"],
                    "customer_ids": [],
                    "product_ids": [],
                    "limit": 10,
                },
                "request_id": "mcp-contract-test",
            },
        )
        assert result.is_error is False
        assert result.structured_content["tool_name"] == "sales_metrics"
