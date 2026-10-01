from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json
import yaml
from typing import List, Optional, Dict, Any
import os
from pathlib import Path
import asyncio
import logging
import sys
import time

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Message(BaseModel):
    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False

class ChatCompletionResponse(BaseModel):
    #id: str
    #object: str
    #created: int
    #model: str
    choices: List[Dict[str, Any]]

# Runtime override for MOCK_DATA_FILE, set via POST /mock/scenario (used by
# e2e tests to switch scripts without restarting the container).
scenario_override: Optional[str] = None


def current_mock_file() -> str:
    return scenario_override or os.getenv("MOCK_DATA_FILE", "default.yaml")


def load_mock_data():
    mock_file = current_mock_file()
    mock_file_path = Path(__file__).parent / "mock_datas" / mock_file

    with open(mock_file_path, 'r', encoding='utf-8') as f:
        logger.info(f"Loading mock data from {mock_file}")
        if mock_file.endswith('.json'):
            return json.load(f)
        else:
            return yaml.safe_load(f)

current_index = 0


class ScenarioRequest(BaseModel):
    file: str


@app.get("/mock/scenario")
async def get_scenario():
    """Inspect the active scenario and replay position."""
    try:
        total = len(load_mock_data())
    except FileNotFoundError:
        total = None
    return {"file": current_mock_file(), "index": current_index, "responses": total}


@app.post("/mock/scenario")
async def set_scenario(request: ScenarioRequest):
    """Switch the active scenario file and reset the replay index."""
    global scenario_override, current_index
    if "/" in request.file or "\\" in request.file:
        raise HTTPException(status_code=400, detail="Invalid scenario file name")
    path = Path(__file__).parent / "mock_datas" / request.file
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Scenario not found: {request.file}")
    scenario_override = request.file
    current_index = 0
    data = load_mock_data()
    logger.info(f"Scenario switched to {request.file} ({len(data)} responses)")
    return {"file": request.file, "index": 0, "responses": len(data)}


@app.post("/mock/reset")
async def reset_scenario():
    """Reset the replay index (and clear any scenario override)."""
    global scenario_override, current_index
    scenario_override = None
    current_index = 0
    return {"file": current_mock_file(), "index": 0}

def _stream_chunks(response: dict, model: str, index: int):
    """Replay a scripted completion as OpenAI-compatible SSE chunks."""
    choice = response["choices"][0]
    message = choice["message"]
    envelope = {
        "id": f"chatcmpl-mock-{index}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }

    def event(delta: dict, finish_reason=None):
        payload = {**envelope, "choices": [{
            "index": 0, "delta": delta, "finish_reason": finish_reason,
        }]}
        return f"data: {json.dumps(payload)}\n\n"

    yield event({"role": "assistant"})
    content = message.get("content") or ""
    for offset in range(0, len(content), 32):
        yield event({"content": content[offset:offset + 32]})
    calls = message.get("tool_calls") or []
    for tool_index, call in enumerate(calls):
        function = call["function"]
        call_id = call.get("id") or f"call-mock-{index}-{tool_index}"
        arguments = function.get("arguments", "")
        yield event({"tool_calls": [{
            "index": tool_index, "id": call_id, "type": "function",
            "function": {"name": function["name"], "arguments": ""},
        }]})
        for offset in range(0, len(arguments), 32):
            yield event({"tool_calls": [{
                "index": tool_index,
                "function": {"arguments": arguments[offset:offset + 32]},
            }]})
    yield event({}, "tool_calls" if calls else "stop")
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest):
    global current_index
    mock_data = load_mock_data()
    if not mock_data:
        current_index = 0
        logger.error("No mock data available")
        raise HTTPException(status_code=500, detail="No mock data available")

    if len(request.messages) == 2 and current_index > 1:
        current_index = 0
        logger.info("Reset index to 0")
    
    delay = float(os.getenv("MOCK_DELAY", "1"))
    if delay > 0:
        logger.debug(f"Applying mock delay of {delay} seconds")
        await asyncio.sleep(delay)
    
    response_index = current_index
    response = mock_data[current_index]
    current_index = (current_index + 1) % len(mock_data)
    logger.info(f"Returning mock response {current_index}/{len(mock_data)}")
    if request.stream:
        return StreamingResponse(
            _stream_chunks(response, request.model, response_index),
            media_type="text/event-stream",
        )
    return response
