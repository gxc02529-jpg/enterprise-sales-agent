"""零依赖压测脚本：基于 asyncio + httpx 打 enterprise-sales-agent。

设计目标
--------
- 不引入 locust/k6 等第三方依赖（本机 pip 源不稳），仅用 httpx（已是项目依赖）。
- 支持「极限吞吐」与「限流生效」两种压测意图：
    * 极限吞吐：用 --gen-tokens N 本地签发 N 个不同 sub 的 JWT，绕过
      API_MAX_CONCURRENT_PER_USER / API_RATE_LIMIT_PER_MINUTE 的 per-user 限制。
    * 限流生效：单 token（默认 dev-admin-token）高频打，预期出现 429 / 503。
- 输出 P50/P95/P99 延迟、QPS、成功率、状态码分布，并可选前后抓取 /metrics 做增量对比。

典型用法
--------
  # 1) 起服务（mock 后端，零外部依赖，最快验证编排+API 吞吐）
  API_MAX_CONCURRENT_PER_USER=200 API_RATE_LIMIT_PER_MINUTE=100000 \
    python -m uvicorn sales_agent.api:app --port 8000 &

  # 2) 极限吞吐：100 并发 / 2000 请求，跨 16 个用户绕过 per-user 限流
  python bench/load_test.py --base-url http://127.0.0.1:8000 \
    --concurrency 100 --requests 2000 --gen-tokens 16 \
    --jwt-secret change-this-in-production

  # 3) 限流生效验证：单 dev token 高频，预期大量 429/503
  python bench/load_test.py --base-url http://127.0.0.1:8000 \
    --concurrency 20 --requests 600 --token dev-admin-token

  # 4) 顺带看 checkpoint / 工具韧性指标（需 metrics_enabled=true）
  python bench/load_test.py --base-url http://127.0.0.1:8000 \
    --concurrency 50 --requests 1000 --gen-tokens 8 \
    --jwt-secret change-this-in-production --metrics
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid
from typing import Any

import httpx
import jwt

ANALYZE_URL = "/v1/analyze"
HEALTH_URL = "/health"
METRICS_URL = "/metrics"

# 覆盖不同路由的查询，贴近真实混合流量
QUERIES = [
    "华东智造 2026 季度 销售额 同比 趋势",  # -> SQL
    "客户网络 关系 谁负责 华南精工",          # -> GRAPH
    "拜访纪要 提到 风险 和 预算",             # -> RAG
    "导出 销售报表 给 华东智造",              # -> EXPORT（+SQL）
    "合同数 排名 按月 统计",                  # -> SQL
]


def gen_tokens(secret: str, n: int, audience: str = "sales-agent") -> list[str]:
    """本地签发 N 个不同 sub 的 JWT，用于绕过 per-user 限流做吞吐压测。"""
    now = int(time.time())
    tokens: list[str] = []
    for i in range(n):
        payload = {
            "sub": f"bench-user-{i}",
            "tenant_id": "bench-tenant",
            "roles": ["sales"],
            "scope_tags": [f"sales:bench-{i}"],
            "exp": now + 3600,
            "aud": audience,
        }
        tokens.append(jwt.encode(payload, secret, algorithm="HS256"))
    return tokens


def parse_metrics(text: str) -> dict[str, float]:
    """极简 Prometheus 文本解析：只取我们关心的计数器/直方图计数。"""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        name, value = parts[0], parts[1]
        if name.startswith(("http_requests_total", "tool_calls_total", "circuit_breaker_state")):
            try:
                out[name] = float(value)
            except ValueError:
                continue
    return out


async def attack_once(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    token: str,
    idx: int,
    results: list[dict[str, Any]],
) -> None:
    body = {
        "request_id": str(uuid.uuid4()),
        "session_id": f"bench-session-{idx % 50}",
        "query": QUERIES[idx % len(QUERIES)],
        "locale": "zh-CN",
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with sem:
        started = time.perf_counter()
        try:
            resp = await client.post(ANALYZE_URL, json=body, headers=headers, timeout=120.0)
            elapsed_ms = (time.perf_counter() - started) * 1000
            results.append(
                {
                    "ok": resp.status_code < 400,
                    "status": resp.status_code,
                    "ms": elapsed_ms,
                    "err": None if resp.status_code < 400 else resp.text[:200],
                }
            )
        except Exception as exc:  # noqa: BLE001 - 压测需捕获所有网络级错误
            elapsed_ms = (time.perf_counter() - started) * 1000
            results.append(
                {"ok": False, "status": 0, "ms": elapsed_ms, "err": type(exc).__name__}
            )


def summarize(results: list[dict[str, Any]], elapsed: float) -> None:
    latencies = [r["ms"] for r in results]
    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    status_hist: dict[int, int] = {}
    for r in results:
        status_hist[r["status"]] = status_hist.get(r["status"], 0) + 1

    print("\n================ 压测结果 ================")
    print(f"总请求数      : {len(results)}")
    print(f"成功          : {len(ok)}  ({len(ok) / max(1, len(results)) * 100:.1f}%)")
    print(f"失败          : {len(failed)}")
    print(f"总耗时        : {elapsed:.2f}s")
    print(f"吞吐 QPS      : {len(results) / elapsed:.1f}")
    if latencies:
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
        p99 = sorted(latencies)[int(len(latencies) * 0.99) - 1]
        print(f"延迟 ms  min : {min(latencies):.1f}")
        print(f"延迟 ms  P50 : {p50:.1f}")
        print(f"延迟 ms  P95 : {p95:.1f}")
        print(f"延迟 ms  P99 : {p99:.1f}")
        print(f"延迟 ms  max : {max(latencies):.1f}")
    print("状态码分布    : " + ", ".join(f"{k}={v}" for k, v in sorted(status_hist.items())))
    if failed:
        print("失败样例(前3):")
        for r in failed[:3]:
            print(f"  status={r['status']} err={r['err']}")
    print("==========================================\n")


async def main_async(args: argparse.Namespace) -> None:
    if args.gen_tokens > 0:
        if not args.jwt_secret:
            raise SystemExit("--gen-tokens 需要 --jwt-secret（与服务端 JWT_SECRET 一致）")
        tokens = gen_tokens(args.jwt_secret, args.gen_tokens)
        print(f"[info] 已本地签发 {len(tokens)} 个 JWT 用于绕过 per-user 限流")
    else:
        tokens = [args.token]
        print(f"[info] 使用单 token={args.token}（per-user 限流会生效）")

    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    sem = asyncio.Semaphore(args.concurrency)
    results: list[dict[str, Any]] = []

    # trust_env=False：避免继承 Bash 环境的透明代理（http_proxy），否则打 localhost 会被劫持
    async with httpx.AsyncClient(base_url=args.base_url, limits=limits, trust_env=False) as client:
        # 预热：不计入统计
        warm = max(1, min(10, args.concurrency))
        await asyncio.gather(
            *(attack_once(client, sem, tokens[0], i, results) for i in range(warm))
        )
        results.clear()

        metrics_before = ""
        if args.metrics:
            try:
                metrics_before = (await client.get(METRICS_URL)).text
            except Exception:  # noqa: BLE001
                metrics_before = ""

        started = time.perf_counter()
        tasks = [
            attack_once(client, sem, tokens[i % len(tokens)], i, results)
            for i in range(args.requests)
        ]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started

        if args.metrics and metrics_before:
            try:
                after = (await client.get(METRICS_URL)).text
                before_map = parse_metrics(metrics_before)
                after_map = parse_metrics(after)
                print("[metrics 增量]")
                for key in sorted(set(before_map) | set(after_map)):
                    delta = after_map.get(key, 0.0) - before_map.get(key, 0.0)
                    print(f"  {key}: +{delta:.0f}")
            except Exception as exc:  # noqa: BLE001
                print(f"[metrics 抓取失败] {type(exc).__name__}")

    summarize(results, elapsed)


def build_parser() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="enterprise-sales-agent 零依赖压测")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--concurrency", type=int, default=20, help="并发请求数")
    p.add_argument("--requests", type=int, default=500, help="总请求数")
    p.add_argument("--token", default="dev-admin-token", help="单 token 模式（限流生效）")
    p.add_argument(
        "--gen-tokens",
        type=int,
        default=0,
        help="本地签发 N 个不同 sub 的 JWT（绕过 per-user 限流做吞吐测试）",
    )
    p.add_argument("--jwt-secret", default="", help="与服务器 JWT_SECRET 一致才能签出有效 token")
    p.add_argument("--metrics", action="store_true", help="前后抓取 /metrics 做增量对比")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(build_parser()))
