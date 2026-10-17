"""Pruebas del tool registrado con el transporte MCP real y un servidor local."""

import asyncio
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

from app.agents.general.tools import get_general_tools
from app.services import parallel_search


class TestParallelTravelSearch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.tool_result = {
            "content": [],
            "structuredContent": {"results": [
                {"title": "Renfe", "url": "https://www.renfe.com/", "excerpts": ["Madrid a Barcelona", "Horarios oficiales"]},
            ]},
            "isError": False,
        }
        self.status = 200
        self.delay = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((dict(self.headers), message))
                method = message["method"]
                if method.startswith("notifications/"):
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {}, "serverInfo": {"name": "test-search", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "web_search", "inputSchema": {"type": "object"}}]}
                else:
                    time.sleep(owner.delay)
                    result = owner.tool_result
                body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
                self.send_response(owner.status if method == "tools/call" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        original = parallel_search.streamable_http_client

        def transport(url, **kwargs):
            self.assertEqual(url, "https://search.parallel.ai/mcp")
            return original(f"http://127.0.0.1:{self.server.server_port}/mcp", **kwargs)

        self.env = patch.dict(os.environ, {"TRAVEL_SEARCH_PROVIDER": "parallel"}, clear=True)
        self.transport = patch.object(parallel_search, "streamable_http_client", side_effect=transport)
        self.env.start()
        self.transport.start()
        self.tool = next(tool for tool in get_general_tools() if tool.name == "travel_search")

    def tearDown(self):
        self.transport.stop()
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    async def invoke(self, text="tren Madrid Barcelona horarios oficiales"):
        return json.loads(await self.tool.ainvoke({"text": text}))

    async def test_registered_tool_normalizes_results_and_sends_anonymous_project_headers(self):
        result = await self.invoke()
        self.assertEqual(result["results"], [{"title": "Renfe", "url": "https://www.renfe.com/", "description": "Madrid a Barcelona\nHorarios oficiales"}])
        self.assertEqual(result["total"], 1)
        call = next(message for _, message in self.requests if message["method"] == "tools/call")
        self.assertEqual(call["params"]["name"], "web_search")
        self.assertEqual(call["params"]["arguments"], {"objective": result["query"], "search_queries": [result["query"]]})
        self.assertTrue(any(message["method"] == "initialize" for _, message in self.requests))
        for headers, _ in self.requests:
            headers = {key.lower(): value for key, value in headers.items()}
            self.assertEqual(headers["user-agent"], parallel_search._USER_AGENT)
            self.assertNotIn("authorization", headers)

    async def test_text_json_response_and_short_query(self):
        self.tool_result = {"content": [{"type": "text", "text": json.dumps({"results": [{"url": "https://example.com", "title": None, "excerpts": []}]})}]}
        result = await self.invoke("Roma")
        self.assertEqual(result["query"], "Roma travel")
        self.assertEqual(result["results"][0]["title"], "")

    async def test_empty_results(self):
        self.tool_result["structuredContent"]["results"] = []
        self.assertEqual((await self.invoke())["total"], 0)

    async def test_result_limit(self):
        self.tool_result["structuredContent"]["results"] *= 8
        self.assertEqual((await self.invoke())["total"], 5)

    async def test_tool_error_preserves_message_without_fallback(self):
        self.tool_result = {"isError": True, "content": [{"type": "text", "text": "Search quota exceeded"}]}
        with patch("app.agents.general.tools.brave_web_search", new_callable=AsyncMock) as brave:
            result = await self.invoke()
        self.assertEqual(result["results"], [])
        self.assertIn("quota", result["error"])
        brave.assert_not_awaited()

    async def test_http_error(self):
        self.status = 503
        result = await self.invoke()
        self.assertEqual(result["results"], [])
        self.assertIn("unavailable", result["error"])

    async def test_malformed_response_is_error(self):
        self.tool_result = {"content": [{"type": "text", "text": "not JSON"}]}
        self.assertIn("error", await self.invoke())

    async def test_total_timeout(self):
        self.delay = .3
        with patch.object(parallel_search, "_REQUEST_TIMEOUT_S", .08):
            result = await self.invoke()
        self.assertEqual(result["results"], [])
        self.assertIn("timed out", result["error"])

    async def test_cancellation_propagates(self):
        self.delay = .3
        task = asyncio.create_task(self.tool.ainvoke({"text": "tren Madrid Barcelona horarios oficiales"}))
        for _ in range(100):
            if any(message["method"] == "tools/call" for _, message in self.requests):
                break
            await asyncio.sleep(.01)
        self.assertTrue(any(message["method"] == "tools/call" for _, message in self.requests))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_unknown_provider_does_not_search(self):
        os.environ["TRAVEL_SEARCH_PROVIDER"] = "typo"
        self.assertIn("TRAVEL_SEARCH_PROVIDER", (await self.invoke())["error"])
        self.assertEqual(self.requests, [])

    async def test_default_and_explicit_brave_preserved(self):
        for value in (None, "brave"):
            if value is None:
                os.environ.pop("TRAVEL_SEARCH_PROVIDER", None)
            else:
                os.environ["TRAVEL_SEARCH_PROVIDER"] = value
            with patch("app.agents.general.tools.is_brave_available", return_value=True), patch("app.agents.general.tools.brave_web_search", new_callable=AsyncMock, return_value={"results": []}) as brave:
                await self.invoke()
                brave.assert_awaited_once()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
