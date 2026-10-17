"""Búsqueda anónima mediante Parallel Search MCP (Streamable HTTP)."""

import asyncio
import json
import logging

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
_USER_AGENT = (
    "travel-assistant/1.0 "
    "(+https://github.com/CharlyMoncada/travel-assitant)"
)
_REQUEST_TIMEOUT_S = 20


async def parallel_web_search(query: str, *, count: int = 5) -> dict:
    """Adapta web_search al contrato {query, results, total} de travel_search.

    No lee claves ni añade autenticación. Cada llamada cierra su sesión MCP;
    el plazo incluye conexión, inicialización, búsqueda y cierre.
    """
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT_S):
            async with httpx.AsyncClient(
                headers={"User-Agent": _USER_AGENT}, timeout=_REQUEST_TIMEOUT_S
            ) as client:
                async with streamable_http_client(
                    _PARALLEL_MCP_URL, http_client=client
                ) as streams:
                    # El transporte también devuelve un callback para consultar la sesión.
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        response = await session.call_tool(
                            "web_search",
                            {"objective": query, "search_queries": [query]},
                        )
                        if response.isError:
                            message = " ".join(
                                block.text for block in response.content
                                if block.type == "text"
                            )
                            return {"query": query, "results": [], "error": message or "Parallel Search failed."}
                        data = response.structuredContent
                        if data is None:
                            data = json.loads("\n".join(
                                block.text for block in response.content
                                if block.type == "text"
                            ))
                        results = [
                            {
                                "title": item.get("title") or "",
                                "url": item["url"],
                                "description": "\n".join(item.get("excerpts") or []),
                            }
                            for item in data["results"][:max(1, min(count, 20))]
                        ]
                        return {"query": query, "results": results, "total": len(results)}
    except TimeoutError:
        return {"query": query, "results": [], "error": "Parallel Search request timed out."}
    except Exception:
        logger.exception("Parallel Search: error al ejecutar la búsqueda")
        return {"query": query, "results": [], "error": "Parallel Search is unavailable. Please try again later."}
