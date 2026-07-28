"""A fake VISTA analyzer implementing the STANDARD contract — for local testing only.

Proves the orchestrator is truly generic: these analyzers share zero code with LogV, yet the
orchestrator discovers, selects, calls and concatenates them exactly the same way. Each fake
exposes the two standard endpoints:

    GET  /<base>/discover  → AnalyzerDiscovery
    POST /<base>/run       → AnalyzerResult  (multipart: file + optional question)

Run two of them (see run_fakes.sh):

    ANALYZER_ID=perf ANALYZER_TITLE="Performance Analyzer" BASE=/perf PORT=8811 \
        WHEN="Use for FortiGate CPU/memory/conserve-mode performance questions." \
        python fakes/fake_analyzer.py

    ANALYZER_ID=eos  ANALYZER_TITLE="End-of-Support Lookup" BASE=/eos PORT=8812 \
        WHEN="Use for end-of-support / lifecycle / EoS date questions about a FortiGate model." \
        python fakes/fake_analyzer.py
"""
from __future__ import annotations

import os

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

ANALYZER_ID = os.getenv("ANALYZER_ID", "perf")
ANALYZER_TITLE = os.getenv("ANALYZER_TITLE", "Performance Analyzer")
BASE = os.getenv("BASE", "/perf").rstrip("/")
PORT = int(os.getenv("PORT", "8811"))
WHEN = os.getenv(
    "WHEN",
    "Use for FortiGate performance questions — CPU/memory pressure, conserve mode, "
    "session or resource exhaustion.",
)
SUMMARY = os.getenv("SUMMARY", f"{ANALYZER_TITLE} (fake analyzer for testing).")

app = FastAPI(title=f"fake-{ANALYZER_ID}")


@app.get(f"{BASE}/discover")
def discover(request: Request):
    root = str(request.base_url).rstrip("/")
    run_path = f"{root}{BASE}/run"
    return {
        "schema_version": "1.0",
        "surface": "analyzer",
        "base_url": f"{root}{BASE}",
        "analyzer": {
            "id": ANALYZER_ID,
            "title": ANALYZER_TITLE,
            "summary": SUMMARY,
            "when_to_use": WHEN,
            "input_types": ["FortiOS/FortiGate log or config file"],
            "supported_log_types": [],
        },
        "query": {
            "method": "POST",
            "path": run_path,
            "content_type": "multipart/form-data",
            "params": [
                {"name": "file", "required": True, "type": "file", "location": "file",
                 "description": "The file to analyze."},
                {"name": "question", "required": False, "type": "string", "location": "form",
                 "description": "The user's question (optional)."},
            ],
            "response": "AnalyzerResult",
        },
    }


@app.post(f"{BASE}/run")
async def run(file: UploadFile = File(...), question: str = Form("")):
    data = await file.read()
    q = (question or "").strip()
    report = (
        f"# {ANALYZER_TITLE}\n\n"
        f"*(fake analyzer — {ANALYZER_ID})*\n\n"
        f"Received **{len(data):,} bytes** from `{file.filename}`.\n\n"
        + (f"**Question:** {q}\n\n" if q else "")
        + f"## Findings\n\nThis is a deterministic stub report from the `{ANALYZER_ID}` "
        f"analyzer, demonstrating that the orchestrator called it generically via its "
        f"discovery contract."
    )
    return JSONResponse({
        "schema_version": "1.0",
        "ok": True,
        "analyzer_id": ANALYZER_ID,
        "title": ANALYZER_TITLE,
        "report_markdown": report,
        "artifacts": {},
        "meta": {"bytes": len(data), "has_question": bool(q)},
        "error": None,
    })


if __name__ == "__main__":
    import uvicorn

    print(f"fake analyzer '{ANALYZER_ID}' ({ANALYZER_TITLE}) → "
          f"http://127.0.0.1:{PORT}{BASE}/discover  |  {BASE}/run")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
