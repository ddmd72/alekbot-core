"""Owner-side HTTP/2 client for the S0 spike (runbook items a and b, and the cold-start tables).

The ID token comes from `--token` or $SPIKE_TOKEN, minted by the owner, e.g.
    gcloud auth print-identity-token --impersonate-service-account=<CALLER_A_SA> --audiences=<SPIKE_URL> --include-email
The URL comes from `--url` or $SPIKE_URL. Nothing is hardcoded. Every subcommand prints one JSON object per row to
stdout and, with `--out FILE`, also writes the full JSON document there (keep it under scripts/memory/, gitignored).

    client.py large --url <SPIKE_URL> --up-mib 50 --down-mib 50 --runs 5          (item a; HTTP/2)
    client.py large --url <SPIKE_URL> --up-mib 33 --down-mib 1 --runs 1 --http1    (the 32 MiB control, expect 413)
    client.py large --url <SPIKE_URL> --up-mib 45 --down-mib 45 --runs 1          (the drive cap)
    client.py burst --url <SPIKE_URL> --sequential 20                             (item b.1)
    client.py burst --url <SPIKE_URL> --parallel 5
    client.py cold  --url <SPIKE_URL> --runs 10 --gap-s 300 --initial-wait-s 900 --label scratch   (item b.3)
"""
import argparse
import asyncio
import contextvars
import hashlib
import json
import math
import os
import sys
import time
from typing import Any, AsyncIterator, Dict, List

import h2.connection
import h2.events
import httpx

from spike_common import mib, stream, stream_sha256

# GOAWAY / connection-terminated events seen by the h2 state machine, attributed to the call that was reading.
_CALL_ID: contextvars.ContextVar[int] = contextvars.ContextVar("spike_call_id", default=-1)
_TERMINATIONS: List[Dict[str, Any]] = []
_original_receive = h2.connection.H2Connection.receive_data


def _recording_receive(self: h2.connection.H2Connection, data: bytes) -> List[Any]:
    events = _original_receive(self, data)
    for event in events:
        if isinstance(event, h2.events.ConnectionTerminated):
            _TERMINATIONS.append({"call": _CALL_ID.get(), "error_code": int(event.error_code),
                                  "last_stream_id": event.last_stream_id, "at": round(time.time(), 3)})
    return events


h2.connection.H2Connection.receive_data = _recording_receive      # type: ignore[method-assign]


def _client(url: str, http1: bool, timeout: float) -> httpx.AsyncClient:
    """HTTP/2 by ALPN on https; prior knowledge (h2c) on http. `--http1` forces HTTP/1.1."""
    if http1:
        return httpx.AsyncClient(timeout=timeout, http1=True, http2=False, trust_env=False)
    prior_knowledge = url.startswith("http://")
    return httpx.AsyncClient(timeout=timeout, http2=True, http1=not prior_knowledge, trust_env=False)


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _result_header(response: httpx.Response) -> Dict[str, Any]:
    raw = response.headers.get("x-spike-result")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


async def _body_stream(seed: str, total: int) -> AsyncIterator[bytes]:
    for piece in stream(seed, total):
        yield piece
        await asyncio.sleep(0)


async def _read_body(response: httpx.Response) -> Dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    async for chunk in response.aiter_bytes():
        digest.update(chunk)
        size += len(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


# -- a: large ---------------------------------------------------------------------------------------------------

async def _large_run(args: argparse.Namespace, run: int) -> Dict[str, Any]:
    up, down = mib(args.up_mib), mib(args.down_mib)
    seed_up, seed_down = f"up{run}", f"down{run}"
    row: Dict[str, Any] = {"run": run, "up_mib": args.up_mib, "down_mib": args.down_mib,
                           "http": "1.1" if args.http1 else "2"}
    started = time.monotonic()
    try:
        async with _client(args.url, args.http1, args.timeout) as client:
            async with client.stream("POST", f"{args.url.rstrip('/')}/echo?down={down}&seed={seed_down}",
                                     content=_body_stream(seed_up, up), headers=_headers(args.token)) as response:
                row["status"] = response.status_code
                row["http_version"] = response.http_version
                if response.status_code == 200:
                    server = _result_header(response)
                    received = await _read_body(response)
                    row["up_hash_equal"] = server.get("sha256") == stream_sha256(seed_up, up)
                    row["down_hash_equal"] = received["sha256"] == stream_sha256(seed_down, down)
                    row["down_bytes"] = received["bytes"]
                    row["rss_peak_mib"] = round((server.get("rss_peak") or 0) / mib(1), 1)
                    row["cgroup_peak_mib"] = round((server.get("cgroup_peak") or 0) / mib(1), 1)
                    row["server_limits"] = server.get("limits")
                else:
                    row["error_body_head"] = (await response.aread())[:120].decode(errors="replace")
    except Exception as exc:   # the class is the result: reset, timeout, protocol error
        row["error_class"] = type(exc).__name__
    row["wall_s"] = round(time.monotonic() - started, 2)
    return row


async def cmd_large(args: argparse.Namespace) -> Dict[str, Any]:
    rows = []
    for run in range(1, args.runs + 1):
        row = await _large_run(args, run)
        print(json.dumps(row), flush=True)
        rows.append(row)
    ok = [r for r in rows if r.get("status") == 200 and r.get("up_hash_equal") and r.get("down_hash_equal")]
    return {"command": "large", "runs": rows, "complete_and_equal": len(ok), "total": len(rows)}


# -- b: burst ---------------------------------------------------------------------------------------------------

async def _recycle_call(args: argparse.Namespace, call_id: int) -> Dict[str, Any]:
    _CALL_ID.set(call_id)
    up = mib(1)
    row: Dict[str, Any] = {"call": call_id}
    started = time.monotonic()
    try:
        async with _client(args.url, False, args.timeout) as client:
            async with client.stream("POST", f"{args.url.rstrip('/')}/recycle", content=_body_stream(f"r{call_id}", up),
                                     headers=_headers(args.token)) as response:
                row["status"] = response.status_code
                row["outcome_header"] = response.headers.get("x-sandbox-outcome")
                row["connection_close_header"] = response.headers.get("connection", "").lower() == "close"
                received = await _read_body(response)
                if response.status_code == 200:
                    server = _result_header(response)
                    row["complete"] = (received["bytes"] == server.get("down_bytes")
                                       and received["sha256"] == server.get("down_sha256"))
                    row["body_seen_by_server_equal"] = server.get("sha256") == stream_sha256(f"r{call_id}", up)
                    row["boot_id"] = server.get("boot_id")
                    row["served_count"] = server.get("served_count")
    except Exception as exc:   # truncated / reset replies land here
        row["error_class"] = type(exc).__name__
    row["wall_s"] = round(time.monotonic() - started, 2)
    row["goaway_events"] = [t for t in _TERMINATIONS if t["call"] == call_id]
    return row


def _classify(row: Dict[str, Any]) -> str:
    if row.get("status") == 200 and row.get("complete"):
        return "complete"
    if row.get("status") == 503 and row.get("outcome_header") == "recycling":
        return "recycling_503"
    if row.get("status") == 429:
        return "platform_429"
    return "truncated_or_other"      # the failures item b is looking for: reset or short reply without `recycling`


async def cmd_burst(args: argparse.Namespace) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    if args.sequential:
        for i in range(args.sequential):
            row = await _recycle_call(args, i)
            print(json.dumps(row), flush=True)
            rows.append(row)
    if args.parallel:
        batch = await asyncio.gather(*[_recycle_call(args, 1000 + i) for i in range(args.parallel)])
        for row in batch:
            print(json.dumps(row), flush=True)
        rows += batch
    counts: Dict[str, int] = {}
    for row in rows:
        counts[_classify(row)] = counts.get(_classify(row), 0) + 1
    boots = [r["boot_id"] for r in rows if r.get("boot_id")]
    summary = {"command": "burst", "sequential": args.sequential, "parallel": args.parallel, "counts": counts,
               "distinct_boot_ids": len(set(boots)), "complete_responses": len(boots),
               "max_served_count": max([r.get("served_count") or 0 for r in rows], default=0),
               "recycling_rate": round(counts.get("recycling_503", 0) / len(rows), 3) if rows else None,
               "goaway_calls": sum(1 for r in rows if r["goaway_events"]),
               "connection_close_calls": sum(1 for r in rows if r.get("connection_close_header"))}
    return {**summary, "rows": rows}


# -- cold start -------------------------------------------------------------------------------------------------

async def _first_byte(args: argparse.Namespace) -> Dict[str, Any]:
    started = time.monotonic()
    url = f"{args.url.rstrip('/')}{args.path}"
    row: Dict[str, Any] = {}
    try:
        async with _client(args.url, False, args.timeout) as client:
            async with client.stream(args.method, url, headers=_headers(args.token)) as response:
                row["status"] = response.status_code
                async for _chunk in response.aiter_raw():
                    break
                row["first_byte_s"] = round(time.monotonic() - started, 3)
    except Exception as exc:
        row["error_class"] = type(exc).__name__
    return row


async def cmd_cold(args: argparse.Namespace) -> Dict[str, Any]:
    if args.initial_wait_s:
        print(json.dumps({"waiting_s": args.initial_wait_s}), flush=True)
        await asyncio.sleep(args.initial_wait_s)
    rows = []
    for run in range(1, args.runs + 1):
        row = {"run": run, **(await _first_byte(args))}
        print(json.dumps(row), flush=True)
        rows.append(row)
        if run < args.runs:
            await asyncio.sleep(args.gap_s)
    times = [r["first_byte_s"] for r in rows if "first_byte_s" in r and r.get("status") == 200]
    table: Dict[str, Any] = {"command": "cold", "label": args.label, "path": args.path, "rows": rows,
                             "samples": len(times)}
    if times:
        table.update(p50_s=_percentile(times, 50), p95_s=_percentile(times, 95), max_s=max(times))
    return table


# -- main -------------------------------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--url", default=os.getenv("SPIKE_URL", ""), help="service URL (or $SPIKE_URL)")
        p.add_argument("--token", default=os.getenv("SPIKE_TOKEN", ""), help="ID token (or $SPIKE_TOKEN)")
        p.add_argument("--timeout", type=float, default=330.0)
        p.add_argument("--out", default="", help="also write the full JSON document here")

    large = sub.add_parser("large")
    common(large)
    large.add_argument("--up-mib", type=int, default=50)
    large.add_argument("--down-mib", type=int, default=50)
    large.add_argument("--runs", type=int, default=5)
    large.add_argument("--http1", action="store_true", help="HTTP/1.1 control (Cloud Run's 32 MiB limit applies)")
    burst = sub.add_parser("burst")
    common(burst)
    burst.add_argument("--sequential", type=int, default=0)
    burst.add_argument("--parallel", type=int, default=0)
    cold = sub.add_parser("cold")
    common(cold)
    cold.add_argument("--runs", type=int, default=10)
    cold.add_argument("--gap-s", type=float, default=300.0)
    cold.add_argument("--initial-wait-s", type=float, default=0.0, help="e.g. 900 so that no instance is warm")
    cold.add_argument("--path", default="/healthz", help="the route to time (the repo image: any cheap route of it)")
    cold.add_argument("--method", default="GET")
    cold.add_argument("--label", default="scratch", help="which image this is: scratch or repo")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.url:
        print("--url or $SPIKE_URL is required", file=sys.stderr)
        return 2
    runner = {"large": cmd_large, "burst": cmd_burst, "cold": cmd_cold}[args.command]
    document = asyncio.run(runner(args))
    summary = {k: v for k, v in document.items() if k not in ("rows", "runs")}
    print(json.dumps({"summary": summary}))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(document, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())

