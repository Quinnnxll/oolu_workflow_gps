#!/usr/bin/env python3
"""Load-test the run pipeline of a live OoLu host.

Points N concurrent workers at a host, each submitting runs and reading
them back, and reports throughput and latency percentiles — the number
you want BEFORE inviting the public, not after.

Usage (against a host you own, never someone else's):

    python scripts/load_test.py --base http://127.0.0.1:8788 \
        --username admin --password ... [--workers 8] [--requests 100]

The intent submitted is a plain no-op-ish task; what's being measured is
the gateway + durable pipeline, not any node's business logic. Needs
httpx (`pip install httpx`).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def _worker(
    client: httpx.AsyncClient,
    *,
    base: str,
    token: str,
    count: int,
    latencies: list[float],
    failures: list[str],
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    for i in range(count):
        started = time.perf_counter()
        try:
            submitted = await client.post(
                f"{base}/v1/runs",
                headers=headers,
                json={"intent": f"load probe {i}: say hello"},
            )
            if submitted.status_code >= 500:
                failures.append(f"submit {submitted.status_code}")
                continue
            run_id = (submitted.json() or {}).get("run_id")
            if run_id:
                fetched = await client.get(
                    f"{base}/v1/runs/{run_id}", headers=headers
                )
                if fetched.status_code >= 500:
                    failures.append(f"fetch {fetched.status_code}")
                    continue
            latencies.append(time.perf_counter() - started)
        except httpx.HTTPError as exc:
            failures.append(type(exc).__name__)


async def run(args) -> int:
    base = args.base.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as client:
        login = await client.post(
            f"{base}/v1/auth/login",
            json={"username": args.username, "password": args.password},
        )
        if login.status_code != 200:
            print(f"login failed: {login.status_code} {login.text[:200]}")
            return 2
        token = login.json()["token"]

        latencies: list[float] = []
        failures: list[str] = []
        per_worker = max(1, args.requests // args.workers)
        started = time.perf_counter()
        await asyncio.gather(
            *(
                _worker(
                    client,
                    base=base,
                    token=token,
                    count=per_worker,
                    latencies=latencies,
                    failures=failures,
                )
                for _ in range(args.workers)
            )
        )
        elapsed = time.perf_counter() - started

    total = len(latencies) + len(failures)
    print(f"requests : {total} ({args.workers} workers)")
    print(f"failures : {len(failures)}"
          + (f" ({', '.join(sorted(set(failures)))})" if failures else ""))
    print(f"elapsed  : {elapsed:.1f}s → {total / elapsed:.1f} req/s")
    if latencies:
        ordered = sorted(latencies)
        print(f"p50      : {statistics.median(ordered) * 1000:.0f} ms")
        print(f"p95      : {ordered[int(len(ordered) * 0.95) - 1] * 1000:.0f} ms")
        print(f"max      : {ordered[-1] * 1000:.0f} ms")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="http(s)://host:port")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--requests", type=int, default=100)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
