#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/.venv/bin/python" "$DIR/fetch_trades.py" "$@"
