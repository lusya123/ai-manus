import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core.config import Settings
from app.domain.models.message import LLMMessage
from app.infrastructure.external.llm.langchain_llm import LangchainLLM


class _ChatCompletionHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", "0")))
        body = json.dumps(
            {
                "id": "chatcmpl-lifecycle",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "shared transport is still usable",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


async def test_closing_one_gateway_does_not_break_next_gateway_request():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatCompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # LangChain caches its default HTTPX client by base URL and timeout.
        # Both gateways therefore use one transport, just like two successive
        # task runners in the backend process.
        settings = Settings(
            _env_file=None,
            api_key="test-only-key",
            api_base=f"http://127.0.0.1:{server.server_port}/v1",
            model_name="gpt-4o-mini",
            model_provider="openai",
        )
        first = LangchainLLM(settings)
        second = LangchainLLM(settings)
        first_transport = first._model.root_async_client._client
        second_transport = second._model.root_async_client._client

        assert first_transport is second_transport
        first_result = await first.ask([LLMMessage.user("first request")])
        await first.aclose()

        assert first._closed is True
        assert second_transport.is_closed is False
        second_result = await second.ask([LLMMessage.user("second request")])
        assert first_result.content == second_result.content

        # Repeated cleanup is idempotent and does not poison the cache for a
        # gateway constructed after the prior tasks have finished.
        await first.aclose()
        await second.aclose()
        third = LangchainLLM(settings)
        assert third._model.root_async_client._client is first_transport
        assert third._model.root_async_client._client.is_closed is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
