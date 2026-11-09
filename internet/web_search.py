import logging

from core import security
from core.tool_result import ToolResult

log = logging.getLogger("evox.web")


def _load_ddgs():
    """Das Paket wurde umbenannt: neu 'ddgs', alt 'duckduckgo_search'. Beides wird unterstützt."""
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS
        return DDGS
    except ImportError:
        return None


class WebSearch:
    @staticmethod
    def search(query: str, max_results: int = 3) -> ToolResult:
        DDGS = _load_ddgs()
        if DDGS is None:
            return ToolResult.failure(
                "Web search is not available: install it with 'pip install duckduckgo-search' (or 'ddgs')."
            )
        try:
            results = list(DDGS().text(query, max_results=max_results))
        except Exception as e:
            log.warning("Web search failed: %s", e)
            return ToolResult.failure(f"The web search failed: {e}")

        if not results:
            return ToolResult.failure(f"The web search for '{query}' returned no results.")

        lines = [f"Search results for '{query}':"]
        for i, res in enumerate(results, start=1):
            lines.append(f"\n[{i}] {res.get('title', '')}\n{res.get('body', '')}\n(Source: {res.get('href', '')})")
        # Webinhalte sind nicht vertrauenswürdig und dürfen keine Aktionen auslösen
        text = security.wrap_untrusted("web_search", "\n".join(lines))
        return ToolResult.success(text, verified=True)
