#!/usr/bin/env python3
"""Publish a completed portfolio research result as an enabled preset."""

from __future__ import annotations

import argparse
import asyncio
import sys


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish portfolio research result to preset.")
    parser.add_argument("--result-id", required=True)
    parser.add_argument("--user-id", required=True, help="Owner user id (JWT id field)")
    parser.add_argument("--status", default="enabled", choices=["draft", "enabled"])
    parser.add_argument(
        "--comment",
        default="",
        help="Preset description / publish comment",
    )
    parser.add_argument("--preset-name", help="Override preset display name")
    return parser.parse_args()


async def _publish(args: argparse.Namespace) -> int:
    from routers.portfolio_research import (
        PublishResearchResultRequest,
        publish_portfolio_research_result,
    )

    request = PublishResearchResultRequest(
        status=args.status,
        comment=args.comment,
        name=args.preset_name,
    )
    response = await publish_portfolio_research_result(
        args.result_id,
        request,
        current_user={"id": args.user_id},
    )
    print(response)
    return 0


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_publish(args))
    except Exception as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
