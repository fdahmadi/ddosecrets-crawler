from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import load_config, resolve_config_path
from .logging_setup import setup_logging
from .pipeline import run_pipeline
from .scheduler import run_scheduler


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ddosecrets-crawler",
        description=(
            "Polite incremental mirror for ddosecrets.org and subdomains "
            "(site pages, data HTTP tree, torrents)."
        ),
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    # Also accept -c before the subcommand: `ddosecrets-crawler -c cfg.yaml run site`
    p.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="Run a sync mode once")
    run.add_argument(
        "mode",
        nargs="?",
        default="all",
        choices=[
            "all",
            "site",
            "data",
            "torrent-index",
            "torrent-start",
            "catalog",
            "full",
            "status",
        ],
    )
    run.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Override site.max_pages for a quick test crawl",
    )
    run.add_argument(
        "--max-file-bytes",
        type=int,
        default=None,
        help="Override data.max_file_bytes (e.g. 10485760 for 10MB test)",
    )
    run.add_argument(
        "--max-indexes",
        type=int,
        default=None,
        help="Override data.max_indexes for a quick data-server smoke test",
    )
    run.add_argument(
        "--no-torrent-download",
        action="store_true",
        help="Index magnets but do not start aria2 downloads",
    )
    run.add_argument(
        "--http-only",
        action="store_true",
        help="Force HTTP downloads even when magnets exist",
    )

    sched = sub.add_parser(
        "schedule", parents=[common], help="Run continuous incremental scheduler"
    )
    sched.add_argument(
        "--once",
        action="store_true",
        help="Run one scheduler cycle then exit",
    )

    sub.add_parser("status", parents=[common], help="Show mirror database statistics")

    serve = sub.add_parser(
        "serve",
        parents=[common],
        help="Start local web UI to browse mirrored catalog/files",
    )
    serve.add_argument("--host", default="0.0.0.0", help="Bind host (default 0.0.0.0)")
    serve.add_argument("--port", type=int, default=8090, help="Bind port (default 8090)")
    return p


def apply_overrides(cfg, args) -> None:
    if getattr(args, "max_pages", None) is not None:
        cfg.site.max_pages = args.max_pages
    if getattr(args, "max_file_bytes", None) is not None:
        cfg.data.max_file_bytes = args.max_file_bytes
    if getattr(args, "max_indexes", None) is not None:
        cfg.data.max_indexes = args.max_indexes
    if getattr(args, "no_torrent_download", False):
        cfg.torrent.use_cli = False
        cfg.torrent.aria2_rpc = None
    if getattr(args, "http_only", False):
        cfg.data.prefer_torrent = False
        cfg.data.http_fallback = True


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        # Allow running from repo root or elsewhere
        alt = Path(__file__).resolve().parents[2] / "config.yaml"
        if alt.exists():
            cfg_path = alt
    base_cfg, local_cfg, active_cfg = resolve_config_path(cfg_path)
    if local_cfg.exists() and active_cfg == local_cfg:
        print(f"Using local overlay config: {active_cfg}")
    cfg = load_config(cfg_path)
    setup_logging(cfg.logging)
    apply_overrides(cfg, args)

    if args.command == "status" or (
        args.command == "run" and args.mode == "status"
    ):
        summary = asyncio.run(run_pipeline(cfg, mode="status"))
        print(json.dumps(summary, indent=2, default=str))
        return 0

    if args.command == "schedule":
        asyncio.run(run_scheduler(cfg, once=args.once))
        return 0

    if args.command == "run":
        summary = asyncio.run(run_pipeline(cfg, mode=args.mode))
        print(json.dumps(summary, indent=2, default=str))
        return 0

    if args.command == "serve":
        from .web.app import run_server

        print(f"Starting UI at http://{args.host}:{args.port}")
        run_server(cfg_path, host=args.host, port=args.port)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())