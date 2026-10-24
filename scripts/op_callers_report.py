#!/usr/bin/env python3
"""Who raised 1Password's CLI-access prompt, grouped by caller.

Reads ~/Library/Logs/yolo-mode-op-callers.jsonl (written by yolo_watch's
_record_op_callers). The caller is the nearest parent that is not a shell,
so `bash -c "op read ..."` inside a Python script is filed under the script.
Usage: op_callers_report.py [--since YYYY-MM-DD]
"""
import json
import sys
from collections import Counter
from pathlib import Path

LOG = Path.home() / "Library/Logs/yolo-mode-op-callers.jsonl"
SHELLS = ("bash", "zsh", "sh", "-zsh", "-bash", "login", "env", "timeout")


def caller(rec: dict) -> str:
    for p in rec.get("parents") or []:
        words = p.split()
        if words and Path(words[0]).name not in SHELLS:
            return p[:120]
    return (rec.get("parents") or ["(no parent)"])[0][:120]


def main() -> None:
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else ""
    if not LOG.exists():
        print(f"no log yet at {LOG}")
        return
    recs = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
    recs = [r for r in recs if r["ts"] >= since]
    print(f"{len(recs)} op calls waiting on the prompt, {recs[0]['ts'] if recs else '-'} to {recs[-1]['ts'] if recs else '-'}")
    by = Counter((caller(r), " ".join(r["op"].split()[1:4])) for r in recs)
    for (who, cmd), n in by.most_common():
        print(f"{n:4d}  {cmd:<40}  {who}")


if __name__ == "__main__":
    main()
