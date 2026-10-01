"""Real API and sandbox smoke for the opt-in single-loop flow."""

import os
import httpx

import pytest
import websockets

from tests.test_e2e_plan_act import (
    CHAT_WS_URL,
    _collect_until_stream_end,
    _create_session,
    _events,
    _frame,
    _join,
    _set_scenario,
    _statuses,
    requires_stack,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("RUN_SINGLE_LOOP_E2E") != "1",
        reason="requires backend AGENT_FLOW=agent_loop and RUN_SINGLE_LOOP_E2E=1",
    ),
]


@requires_stack
async def test_single_loop_real_work_report_and_streamed_final():
    _set_scenario("single_loop_e2e.yaml")
    session_id = _create_session()

    async with websockets.connect(CHAT_WS_URL) as ws:
        await _join(ws, session_id)
        await ws.send(_frame("chat", session_id, message="Run the single loop smoke"))
        frames = await _collect_until_stream_end(ws)

    plans = _events(frames, "plan")
    assert plans and plans[0]["steps"][0]["id"] == "1"
    assert all(step["status"] == "completed" for step in plans[-1]["steps"])
    tools = _events(frames, "tool")
    assert any(tool["function"] == "shell_exec" for tool in tools)
    assert not any(tool["function"] == "plan_report" for tool in tools)

    final_text = (
        "Single loop E2E smoke finished after the real sandbox command returned "
        "single-loop-smoke-ok. The verified plan is complete."
    )
    finals = [m for m in _events(frames, "message") if m["content"] == final_text]
    assert len(finals) == 1
    deltas = _events(frames, "message_delta")
    assert deltas, "streaming final preview was not emitted"
    assert all(d["message_id"] == finals[0]["message_id"] for d in deltas)
    assert _statuses(frames)[-1] == "completed"
    assert _events(frames, "done")


@requires_stack
async def test_single_loop_wait_resume_reuses_successful_shell_work():
    _set_scenario("single_loop_wait_e2e.yaml")
    session_id = _create_session()

    async with websockets.connect(CHAT_WS_URL) as ws:
        await _join(ws, session_id)
        await ws.send(_frame("chat", session_id, message="Verify then ask me"))
        first = await _collect_until_stream_end(ws)

        assert _events(first, "wait")
        assert _statuses(first)[-1] == "waiting"
        first_shell = [tool for tool in _events(first, "tool") if tool["function"] == "shell_exec"]
        assert first_shell and any(tool["status"] == "called" for tool in first_shell)
        assert not _events(first, "done")

        await ws.send(_frame("chat", session_id, message="Option B"))
        second = await _collect_until_stream_end(ws)

    second_shell = [tool for tool in _events(second, "tool") if tool["function"] == "shell_exec"]
    assert second_shell == [], "resumed turn repeated the successful shell side effect"
    assert any(
        message["content"] == "Single loop wait resumed with option B; the previously verified shell command was not repeated."
        for message in _events(second, "message")
    )
    plans = _events(second, "plan")
    assert plans and all(step["status"] == "completed" for step in plans[-1]["steps"])
    assert _statuses(second)[-1] == "completed"
    assert _events(second, "done")

    scenario = httpx.get("http://localhost:8090/mock/scenario", timeout=5).json()
    assert scenario["index"] == 0, "all six scripted responses should be consumed exactly once"
