"""Fail on error-level results in the SARIF files of a directory (CodeQL's output).

Every result becomes a GitHub annotation (readable without a token, like the other CI
failures); an `error` result fails the step. Results suppressed in the source (a
reviewed false positive) carry SARIF `suppressions` and are skipped.

    python3 .github/scripts/sarif_gate.py <dir-with-sarif-files>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(folder: str) -> int:
    errors = 0
    files = sorted(Path(folder).glob("*.sarif"))
    if not files:
        print(f"::error title=sarif gate::no SARIF file in {folder}")
        return 1
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8"))
        for run in doc.get("runs", []):
            tool = run.get("tool", {})
            components = [tool.get("driver", {}), *(tool.get("extensions") or [])]  # CodeQL: packs
            rules = {r.get("id"): r for c in components for r in (c.get("rules") or [])}
            for res in run.get("results", []):
                if res.get("suppressions"):
                    continue
                rule = rules.get(res.get("ruleId"), {})
                level = res.get("level") or rule.get("defaultConfiguration", {}).get("level", "warning")
                loc = (res.get("locations") or [{}])[0].get("physicalLocation", {})
                path = loc.get("artifactLocation", {}).get("uri", "")
                line = loc.get("region", {}).get("startLine", 1)
                msg = res.get("message", {}).get("text", "")
                kind = "error" if level == "error" else "warning"
                errors += kind == "error"
                print(f"::{kind} file={path},line={line},title={escape(str(res.get('ruleId')))}::{escape(msg[:900])}")
    print(f"{errors} error-level finding(s) in {len(files)} SARIF file(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
