#!/usr/bin/env python3
import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="LabelFlow one-click launcher")
    parser.add_argument("--host", default="0.0.0.0", help="Server host, default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=8000, help="Server port, default: 8000")
    parser.add_argument("--reload", action="store_true", help="Enable auto reload for development")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args()

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
