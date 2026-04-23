"""
agent/tools/web_search.py

Implements WebSearchTool using DuckDuckGo Lite (no API key required).

The tool is disabled by default because the agent is designed for offline
local use and outbound HTTP requests may not be desired.  Set the
environment variable WEB_SEARCH_ENABLED=1 to enable it.

When disabled:
- The tool's description is set to a notice so the planner knows not to
  use it (it will be absent from the tool list in a future refactor).
- run() returns a clear error rather than silently returning nothing.

When enabled:
- Sends a POST to https://lite.duckduckgo.com/lite/ and parses the
  lightweight HTML response to extract titles, URLs, and snippets.
- Returns a JSON array of up to max_results results.

No additional dependencies are required beyond the already-listed
'requests' package.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("WEB_SEARCH_ENABLED", "").lower() in ("1", "true", "yes")
_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_MAX_RESULTS = 5


# ---------------------------------------------------------------------------
# Search backend
# ---------------------------------------------------------------------------


def _ddg_search(query: str, max_results: int = _MAX_RESULTS) -> List[Dict[str, str]]:
    """Scrape DuckDuckGo Lite and return structured result dicts.

    DuckDuckGo Lite is a minimal HTML page served via POST with a 'q'
    form field.  Results are in a simple table with class='result-link'
    anchors and class='result-snippet' cells — stable enough for scraping.
    """
    resp = requests.post(
        _DDG_LITE_URL,
        data={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
        timeout=10,
    )
    resp.raise_for_status()
    html = resp.text

    # DDG lite anchor format: <a rel="nofollow" href="URL" class='result-link'>Title</a>
    link_pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]+class=["\']result-link["\'][^>]*>(.*?)</a>',
        re.DOTALL,
    )
    # Snippet format: <td ... class='result-snippet'>...</td>
    snippet_pattern = re.compile(
        r"class='result-snippet'[^>]*>(.*?)</td>",
        re.DOTALL,
    )

    links = link_pattern.findall(html)
    snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippet_pattern.findall(html)]

    results = []
    for i, (url, raw_title) in enumerate(links[:max_results]):
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        snippet = snippets[i] if i < len(snippets) else ""
        results.append({"title": title, "url": url, "snippet": snippet})

    return results


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class WebSearchTool:
    """Perform a web search via DuckDuckGo Lite and return summarized results.

    Requires WEB_SEARCH_ENABLED=1.  When disabled the tool reports
    unavailability so the planner does not attempt to use it.
    """

    description = (
        "Perform a web search and return summarized results."
        if _ENABLED
        else "Web search (currently disabled — set WEB_SEARCH_ENABLED=1 to enable)."
    )

    def run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not _ENABLED:
            return {
                "stdout": "",
                "stderr": (
                    "Web search is disabled. "
                    "Set WEB_SEARCH_ENABLED=1 to enable outbound search."
                ),
                "returncode": 1,
            }

        query = args.get("query")
        if not query:
            return {"stdout": "", "stderr": "Missing required argument: query", "returncode": 1}

        try:
            results = _ddg_search(query)
            return {"stdout": json.dumps(results, indent=2), "stderr": "", "returncode": 0}
        except Exception as exc:
            logger.warning("Web search failed: %s", exc)
            return {"stdout": "", "stderr": f"Web search failed: {exc}", "returncode": 1}
