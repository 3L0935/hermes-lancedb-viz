#!/usr/bin/env python3
"""Run backup-first LanceDB compaction only when the read-only plan recommends it."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(url: str, *, data: dict | None = None, timeout: float = 30.0) -> dict:
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("maintenance endpoint did not return a JSON object")
    return payload


def run(base_url: str, *, timeout: float = 30.0) -> tuple[int, dict]:
    base_url = base_url.rstrip("/")
    plan = request_json(
        f"{base_url}/api/maintenance/compact/plan", timeout=timeout
    )
    if plan.get("error"):
        return 2, {"action": "plan_failed", "plan": plan}
    if plan.get("recommended") is not True:
        return 0, {"action": "not_needed", "plan": plan}

    result = request_json(
        f"{base_url}/api/maintenance/compact",
        data={"confirmed": True},
        timeout=timeout,
    )
    if result.get("code") in {"compaction_in_progress", "maintenance_lock_busy"}:
        return 0, {"action": "deferred", "plan": plan, "result": result}
    if result.get("success") is not True:
        return 1, {"action": "failed", "plan": plan, "result": result}
    return 0, {"action": "compacted", "plan": plan, "result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LANCEDB_VIZ_URL", "http://127.0.0.1:7777"),
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    try:
        exit_code, report = run(args.base_url, timeout=args.timeout)
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"action": "request_failed", "error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
