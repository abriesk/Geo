#!/usr/bin/env python3
"""Emit JSON Schema files for every SS6 contract into contracts/schemas/.

Run after any change to geohazard_contracts; commit the output. These files
are the language-neutral form of the contracts (pasted into coding sessions
per SS11.3 rule 1) and can validate payloads outside Python.
"""
import json
from pathlib import Path

from geohazard_contracts import (
    AnalysisTaskMessage, DownloadTaskMessage, ProgressMessage,
    QueryPayload, ResultJson, ResultMessage,
)

OUT = Path(__file__).resolve().parent.parent / "contracts" / "schemas"
MODELS = {
    "query_payload": QueryPayload,           # SS6.1
    "result_json": ResultJson,               # SS6.3
    "task_download": DownloadTaskMessage,    # SS6.4
    "task_analysis": AnalysisTaskMessage,    # SS6.4
    "progress_message": ProgressMessage,     # SS6.4
    "result_message": ResultMessage,         # SS6.4
}

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        schema = model.model_json_schema()
        path = OUT / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path.relative_to(OUT.parent.parent)}")

if __name__ == "__main__":
    main()
