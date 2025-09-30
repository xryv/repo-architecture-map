#!/usr/bin/env python3
# repo-architecture-map — CLI entry
# Zero dependencies. Python 3.8+. Cross-platform.
# Usage:
#   python /path/to/repo-architecture-map/cli.py --dry-run
#   python /path/to/repo-architecture-map/cli.py --out ARCHITECTURE.md
#   python /path/to/repo-architecture-map/cli.py --format mermaid --theme auto --out ARCHITECTURE.mmd

import argparse, os, sys
from pathlib import Path
import core

VERSION = "0.2.0"

def main():
    ap = argparse.ArgumentParser(
        description="Generate a Mermaid architecture diagram from Docker Compose, Kubernetes, package.json and .env."
    )
    ap.add_argument("--root", default=".", help="Repo root to scan (default: .)")
    ap.add_argument("--out", default=None, help="Output file path (Markdown or raw Mermaid). If omitted, prints to stdout.")
    ap.add_argument("--format", choices=["md","mermaid"], default="md", help="Output wrapper: Markdown+Mermaid (md) or raw Mermaid (mermaid).")
    ap.add_argument("--theme", choices=["auto","dark","light","plain"], default="auto", help="Diagram theme presets (auto picks dark/light from env).")
    ap.add_argument("--style", choices=["fancy","plain"], default="fancy", help="Fancy adds classDefs, legend, and visual cues.")
    ap.add_argument("--legend", action="store_true", help="Force include legend (fancy style implies legend by default).")
    ap.add_argument("--version", action="store_true", help="Print version and exit.")
    args = ap.parse_args()

    if args.version:
        print(VERSION); return 0

    root = os.path.abspath(args.root)
    graph = core.scan_repo(root)
    mermaid = core.build_mermaid(graph, theme=args.theme, style=args.style, include_legend=(args.legend or args.style=="fancy"))

    if args.format == "md":
        content = core.wrap_markdown(mermaid, graph.summary)
    else:
        content = mermaid

    if args.out:
        Path(args.out).write_text(content, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(content)
    return 0

if __name__ == "__main__":
    sys.exit(main())
