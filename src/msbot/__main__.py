from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .config import Settings
from .forms import IssueForm


def main():
    parser = argparse.ArgumentParser(description="MindStudio Bug Issue close audit")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--config", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    render = commands.add_parser("render-template")
    render.add_argument("template")
    args = parser.parse_args()
    if args.command == "render-template":
        print(IssueForm.parse(Path(args.template).read_text(encoding="utf-8")).render(), end="")
        return
    logging.basicConfig(level=logging.INFO)
    # httpx INFO logs contain full URLs, including GitCode query tokens and Feishu hook secrets.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    from .app import create_app

    uvicorn.run(create_app(Settings.load(args.config)), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
