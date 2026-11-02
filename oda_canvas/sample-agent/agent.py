"""Sample-A2A-T-Agent: an A2A + A2A-T agent that answers every message with an LLM.

* A2A: built on ``a2a-sdk`` (1.x) the way the SDK samples do it: an ``AgentExecutor`` driven by
  ``DefaultRequestHandler`` and a ``TaskUpdater`` (submitted -> working -> artifact -> completed/failed/rejected),
  served over JSON-RPC (v1.0, with v0.3 compatibility) and the public Agent Card.
* A2A-T: the card declares the Task-T extension. When a client activates it (``A2A-Extensions`` header) the business
  prompt travels in the message metadata under the extension URI and is checked with ``a2a_t`` before it reaches the
  model (a non-compliant prompt is REJECTED). Terminal status updates also carry the ``__sdk_event__`` metadata that
  the OpenAN Orchestration Center (Host Agent protocol) reads to end a run.
* LLM: any OpenAI-compatible chat-completions endpoint (``LLM_BASE_URL`` / ``LLM_MODEL`` / ``LLM_API_KEY``).
"""

from __future__ import annotations

import json
import logging
import os
import time

import uvicorn
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import AgentCard, Part, Task, TaskState, TaskStatus
from google.protobuf.json_format import MessageToDict, ParseDict
from openai import AsyncOpenAI
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

logger = logging.getLogger("sample-a2a-t-agent")

AGENT_NAME = os.environ.get("AGENT_NAME", "Sample-A2A-T-Agent")
PORT = int(os.environ.get("PORT", "8080"))
# In-cluster URL of the JSON-RPC endpoint, published in the Agent Card.
PUBLIC_URL = os.environ.get("AGENT_PUBLIC_URL", f"http://localhost:{PORT}/a2a")
# Paths the JSON-RPC endpoint and the card are served under. "/a2a" is the in-cluster path; the Istio gateway
# forwards the full ExposedAPI path, so that one is served too.
RPC_PATHS = [p for p in os.environ.get("A2A_RPC_PATHS", "/a2a").split(",") if p]

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "default")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or "EMPTY"  # keyless endpoints ignore it
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "300"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))
# vLLM/Qwen3-style reasoning models answer in a hidden "reasoning" field unless thinking is switched off.
LLM_ENABLE_THINKING = os.environ.get("LLM_ENABLE_THINKING", "false").lower() == "true"
SYSTEM_PROMPT = os.environ.get(
    "LLM_SYSTEM_PROMPT",
    "You are Sample-A2A-T-Agent, a helpful assistant in a telecom multi-agent system. Answer the user's message "
    "concisely and accurately.",
)

TASK_T_URI = "https://projects.tmforum.org/a2aproject/telecommunication/extensions/Task-T/v1"

AGENT_CARD = {
    "name": AGENT_NAME,
    "description": "A2A / A2A-T agent that passes every received message to an LLM and replies with its answer",
    "version": "1.0.0",
    "provider": {"organization": "OpenAN", "url": "https://github.com/project-openan"},
    "supportedInterfaces": [{"url": PUBLIC_URL, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {
        "streaming": True,
        "pushNotifications": False,
        "extensions": [{
            "uri": TASK_T_URI,
            "description": "A2A-T Task-T: structured business prompts are carried in the message metadata under "
                           "this URI; compliance is checked before the prompt reaches the model.",
            "required": False,
        }],
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{
        "id": "llm-chat",
        "name": "LLM chat",
        "description": "Answers a message (plain text or an A2A-T Task-T prompt) with a large language model",
        "tags": ["llm", "chat", "a2a-t"],
        "examples": ["Summarise the main causes of packet loss on an IP backbone link."],
    }],
}


def build_agent_card() -> AgentCard:
    return ParseDict(AGENT_CARD, AgentCard())


class LLM:
    """Thin OpenAI-compatible chat client."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=LLM_TIMEOUT, max_retries=1)

    async def answer(self, text: str) -> str:
        extra = {} if LLM_ENABLE_THINKING else {"chat_template_kwargs": {"enable_thinking": False}}
        completion = await self._client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}],
            max_tokens=LLM_MAX_TOKENS,
            extra_body=extra,
        )
        message = completion.choices[0].message
        reply = (message.content or "").strip()
        if not reply:
            raise RuntimeError(
                f"the model returned no content (finish_reason={completion.choices[0].finish_reason}); "
                "raise LLM_MAX_TOKENS or disable thinking")
        return reply


def _merge_system_messages() -> None:
    """Compatibility shim: the SDK's OpenAI client sends several leading ``system`` messages, which strict chat
    templates (e.g. Qwen on vLLM: "System message must be at the beginning") reject. Merge them into one."""
    from a2a_t.llm.providers.openai import OpenAIClient

    original = OpenAIClient._build_structured_messages
    if getattr(original, "_merged", False):
        return

    def merged(self, messages, json_schema):  # noqa: ANN001
        built = original(self, messages, json_schema)
        system = "\n\n".join(m["content"] for m in built if m["role"] == "system")
        return [{"role": "system", "content": system}, *[m for m in built if m["role"] != "system"]]

    merged._merged = True  # type: ignore[attr-defined]
    OpenAIClient._build_structured_messages = merged  # type: ignore[method-assign]


class TaskTChecker:
    """A2A-T server-side compliance check of Task-T prompts (``a2a_t.server.A2ATServer``)."""

    def __init__(self) -> None:
        from pathlib import Path
        from a2a_t.server.a2at_server import A2ATServer
        # The SDK reads a .env file (not the process environment): render one from the LLM_* settings.
        env_file = Path(os.environ.get("A2AT_ENV_FILE", "/tmp/a2at.env"))
        env_file.write_text("\n".join([
            "A2AT_LANGUAGE=en-US",
            "A2AT_PROMPT_SOURCE_TYPE=packaged",
            "A2AT_LLM_PROVIDER=openai",
            f"A2AT_LLM_MODEL={LLM_MODEL}",
            f"A2AT_LLM_API_KEY={LLM_API_KEY}",
            f"A2AT_LLM_BASE_URL={LLM_BASE_URL}",
            f"A2AT_LLM_TIMEOUT_SECONDS={int(LLM_TIMEOUT)}",
            "",
        ]), encoding="utf-8")
        _merge_system_messages()
        self._server = A2ATServer(env_path=env_file)

    def check(self, prompt_text: str) -> tuple[bool, str]:
        result = self._server.check_task_prompt(processed_prompt_text=prompt_text)
        return bool(result.success), "" if result.success else str(result.failure)


def sdk_event(kind: str, **data: object) -> dict[str, str]:
    """Status-update metadata read by the OpenAN Orchestration Center (A2A-T task updates)."""
    return {"__sdk_event__": json.dumps({"type": kind, "data": data, "timestamp": time.time()}, ensure_ascii=False)}


class LLMAgentExecutor(AgentExecutor):
    def __init__(self, llm: LLM, checker: TaskTChecker | None) -> None:
        self._llm = llm
        self._checker = checker

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id, message = context.task_id, context.context_id, context.message
        if not task_id or not context_id or message is None:
            return
        if context.current_task is None:  # first event of a new task must be the Task itself
            await event_queue.enqueue_event(Task(
                id=task_id, context_id=context_id, history=[message],
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
        updater = TaskUpdater(event_queue, task_id, context_id)

        text = context.get_user_input()
        if TASK_T_URI in context.requested_extensions:
            metadata = MessageToDict(message.metadata) if message.HasField("metadata") else {}
            prompt = str(metadata.get(TASK_T_URI) or "")
            if prompt:
                if self._checker is not None:
                    ok, failure = self._checker.check(prompt)
                    if not ok and "invocation failed" in failure:  # the check could not run: not a verdict
                        error = f"Task-T prompt check could not run: {failure}"
                        await updater.update_status(
                            TaskState.TASK_STATE_FAILED, updater.new_agent_message([Part(text=error)]),
                            metadata=sdk_event("error", error=error))
                        return
                    if not ok:
                        await updater.update_status(
                            TaskState.TASK_STATE_REJECTED,
                            updater.new_agent_message([Part(text=f"Task-T prompt rejected: {failure}")]),
                            metadata=sdk_event("error", error=f"Task-T prompt rejected: {failure}"))
                        return
                text = prompt
        if not text.strip():
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED, updater.new_agent_message([Part(text="The message has no text.")]),
                metadata=sdk_event("error", error="The message has no text."))
            return

        await updater.start_work(updater.new_agent_message([Part(text="Asking the LLM...")]))
        try:
            reply = await self._llm.answer(text)
        except Exception as exc:  # the client gets a FAILED task, not a transport error
            logger.exception("LLM call failed")
            await updater.update_status(
                TaskState.TASK_STATE_FAILED, updater.new_agent_message([Part(text=f"LLM call failed: {exc}")]),
                metadata=sdk_event("error", error=f"LLM call failed: {exc}"))
            return
        await updater.add_artifact([Part(text=reply)], name="llm-response", last_chunk=True)
        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED, updater.new_agent_message([Part(text=reply)]),
            metadata=sdk_event("complete", result=reply))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
        await updater.cancel()


def build_app() -> Starlette:
    card = build_agent_card()
    try:
        checker: TaskTChecker | None = TaskTChecker()
    except Exception:  # keep serving plain A2A if the A2A-T resources cannot be loaded
        logger.exception("A2A-T Task-T checker unavailable; Task-T prompts will not be validated")
        checker = None
    handler = DefaultRequestHandler(
        agent_executor=LLMAgentExecutor(LLM(), checker), task_store=InMemoryTaskStore(), agent_card=card)
    routes = [Route("/healthz", lambda _request: PlainTextResponse("ok"))]
    routes += create_agent_card_routes(card)
    for path in RPC_PATHS:
        routes += create_jsonrpc_routes(handler, rpc_url=path, enable_v0_3_compat=True)
        routes += create_agent_card_routes(card, card_url=f"{path.rstrip('/')}/.well-known/agent-card.json")
    return Starlette(routes=routes)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, log_level="info")
