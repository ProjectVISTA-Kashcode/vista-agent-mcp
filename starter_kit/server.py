"""Minimal example MCP server — build tools our agents can call.

This runs standalone in YOUR repo and YOUR stack. Our agent connects to it over
HTTP and calls the tools you expose here; you never need our source code. Clone
this folder, extend `filter_log` or add your own `@mcp.tool` functions, deploy it
somewhere our backend can reach, and give us the URL + a token (see README.md).

Run:
    pip install -r requirements.txt
    python server.py            # serves at http://0.0.0.0:8100/mcp/
"""

import os
import re

import httpx
from fastmcp import FastMCP

mcp = FastMCP("example-tools")

# The agent injects the log URL automatically; a tool should never read a log
# larger than this into memory (guard against a huge upload). Tune as needed.
_MAX_LOG_BYTES = 50 * 1024 * 1024  # 50 MB


async def _fetch_log(source_url: str) -> str:
    """Download the log the agent handed us. `source_url` is a short-lived,
    signed URL — fetch it now, don't store it (it expires in minutes)."""
    timeout = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", source_url) as resp:
            resp.raise_for_status()
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_LOG_BYTES:
                    raise ValueError("log exceeds the size this tool will process")
                chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


@mcp.tool
async def filter_log(source_url: str, severity: str = "error", pattern: str = "") -> str:
    """Reduce a log to the lines that matter before the agent reads it.

    Args:
        source_url: Signed URL to the uploaded log. THE AGENT FILLS THIS
            automatically — the model never supplies it. Just fetch it.
        severity: Keep lines at or above this level. One of:
            error | warning | info (case-insensitive).
        pattern: Optional regular expression; when set, a line is kept only if
            it ALSO matches this pattern.

    Returns:
        The filtered log text (kept lines joined by newlines), or a short note
        when nothing matched.
    """
    text = await _fetch_log(source_url)

    # Severity ladder: keep the requested level and everything more severe.
    ladder = ["error", "warning", "info"]
    sev = severity.lower().strip()
    keep_levels = ladder[: ladder.index(sev) + 1] if sev in ladder else ["error"]
    sev_re = re.compile("|".join(rf"\b{lvl}\b" for lvl in keep_levels), re.IGNORECASE)
    pat_re = re.compile(pattern) if pattern else None

    kept = [
        line for line in text.splitlines()
        if sev_re.search(line) and (pat_re is None or pat_re.search(line))
    ]
    if not kept:
        return f"No lines matched severity>={sev or 'error'}" + (f" and /{pattern}/" if pattern else "") + "."
    # Cap the returned volume so the agent's context stays sane.
    head = kept[:2000]
    out = "\n".join(head)
    if len(kept) > len(head):
        out += f"\n… ({len(kept) - len(head)} more matching lines omitted)"
    return out


if __name__ == "__main__":
    # HTTP transport (matches how the agent connects). The served path is /mcp/,
    # so the URL to give us is  http://<host>:<port>/mcp/
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8100")))
