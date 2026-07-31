"""A fake VISTA analyzer with a DELIBERATELY DIFFERENT API shape — proves dynamic discovery.

``fake_analyzer.py`` uses the familiar shape (``file`` + optional ``question`` at
``<base>/run``). This one follows the *same standard contract* but advertises a different
request surface:

    * the file param is called **payload**, not ``file``
    * the question param is called **query**, and it is **required**
    * there is an extra **required** ``mode`` param the caller has to choose (quick | deep)
    * the query path is **<base>/v2/analyze**, not ``<base>/run``

Nothing in VISTA-MCP knows any of that. The orchestrator fetches this discovery, the **AI
Controller** reads the contract and plans the call accordingly, and the plan is validated against
the same document before it is sent. The report this analyzer returns **echoes exactly what it
received**, so a test can assert the orchestrator adapted correctly.

Run it:

    ANALYZER_ID=perf2 ANALYZER_TITLE="Performance Analyzer v2" BASE=/perf2 PORT=8813 \
        WHEN="Use for FortiGate CPU/memory/conserve-mode performance questions." \
        python fakes/fake_analyzer_v2.py
"""
from __future__ import annotations

import os

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

ANALYZER_ID = os.getenv("ANALYZER_ID", "perf2")
ANALYZER_TITLE = os.getenv("ANALYZER_TITLE", "Performance Analyzer v2")
BASE = os.getenv("BASE", "/perf2").rstrip("/")
PORT = int(os.getenv("PORT", "8813"))
WHEN = os.getenv(
    "WHEN",
    "Use for FortiGate performance questions — CPU/memory pressure, conserve mode, session or "
    "resource exhaustion. Do NOT use for routine SLA/traffic/event log questions.",
)

app = FastAPI(title=f"fake-{ANALYZER_ID}")


@app.get(f"{BASE}/discover")
def discover(request: Request):
    root = str(request.base_url).rstrip("/")
    return {
        "schema_version": "1.0",
        "surface": "analyzer",
        "base_url": f"{root}{BASE}",
        "analyzer": {
            "id": ANALYZER_ID,
            "title": ANALYZER_TITLE,
            "summary": f"{ANALYZER_TITLE} — same contract, different request shape.",
            "when_to_use": WHEN,
            "input_types": ["FortiOS/FortiGate log or config file"],
            "supported_log_types": [],
        },
        "query": {
            "method": "POST",
            "path": f"{root}{BASE}/v2/analyze",          # NOT /run
            "content_type": "multipart/form-data",
            "params": [
                {"name": "payload", "required": True, "type": "file", "location": "file",
                 "description": "The file to analyze."},
                {"name": "query", "required": True, "type": "string", "location": "form",
                 "description": "The user's question. Required by this version of the API."},
                # A NEW required param. It advertises a `default`, so even the no-AI fallback
                # path stays correct; the AI Controller may override it from the description.
                {"name": "mode", "required": True, "type": "string", "location": "form",
                 "default": "quick",
                 "description": "Analysis depth: 'quick' for a fast scan, 'deep' for a full "
                                "correlation pass. Choose 'deep' when the question mentions a "
                                "specific device, interface or metric."},
            ],
            "response": "AnalyzerResult",
        },
    }


@app.post(f"{BASE}/v2/analyze")
async def analyze(payload: UploadFile = File(...), query: str = Form(...), mode: str = Form(...)):
    data = await payload.read()
    report = (
        f"# {ANALYZER_TITLE}\n\n"
        f"*(fake analyzer — {ANALYZER_ID}, contract v2)*\n\n"
        f"Received **{len(data):,} bytes** from `{payload.filename}` on the **payload** param.\n\n"
        f"- `query` = {query!r}\n"
        f"- `mode`  = {mode!r}\n\n"
        f"## Findings\n\nThe orchestrator called this analyzer using **only** what the discovery "
        f"document advertised — a renamed file param, a renamed+required question param, a new "
        f"required `mode` param, and a different path. No VISTA-MCP code knows any of that."
    )
    return JSONResponse({
        "schema_version": "1.0",
        "ok": True,
        "analyzer_id": ANALYZER_ID,
        "title": ANALYZER_TITLE,
        "report_markdown": report,
        "artifacts": {},
        # echoed back so an end-to-end test can assert exactly what was sent
        "meta": {"bytes": len(data), "file_param": "payload", "query": query, "mode": mode},
        "error": None,
    })


if __name__ == "__main__":
    import uvicorn

    print(f"fake analyzer v2 '{ANALYZER_ID}' ({ANALYZER_TITLE}) → "
          f"http://127.0.0.1:{PORT}{BASE}/discover  |  {BASE}/v2/analyze")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
