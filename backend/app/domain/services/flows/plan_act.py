import logging
from app.domain.services.flows.base import BaseFlow
from app.domain.models.message import Message
from typing import AsyncGenerator, Optional
from enum import Enum
from app.domain.models.event import (
    BaseEvent,
    PlanEvent,
    PlanStatus,
    MessageEvent,
    DoneEvent,
    TitleEvent,
    ErrorEvent,
)
from app.domain.models.plan import ExecutionStatus
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.external.sandbox import Sandbox
from app.domain.external.browser import Browser
from app.domain.external.search import SearchEngine
from app.domain.external.llm import LLM
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.repositories.session_repository import SessionRepository
from app.domain.repositories.project_repository import ProjectRepository
from app.domain.models.session import SessionStatus
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.services.tools.shell import ShellToolkit
from app.domain.services.tools.browser import BrowserToolkit
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.message import MessageToolkit
from app.domain.services.tools.search import SearchToolkit
from app.domain.services.tools.preview import PreviewToolkit
from app.domain.services.prompts.runtime import build_runtime_environment_prompt

logger = logging.getLogger(__name__)

class AgentStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    UPDATING = "updating"

class PlanActFlow(BaseFlow):
    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        session_id: str,
        session_repository: SessionRepository,
        sandbox: Sandbox,
        browser: Browser,
        mcp_tool: MCPToolkit,
        llm: LLM,
        search_engine: Optional[SearchEngine] = None,
        project_repository: Optional[ProjectRepository] = None,
    ):
        self._agent_id = agent_id
        self._repository = agent_repository
        self._session_id = session_id
        self._session_repository = session_repository
        self._project_repository = project_repository
        self._llm = llm
        self.status = AgentStatus.IDLE
        self.plan = None

        tools = [
            ShellToolkit(sandbox),
            BrowserToolkit(browser),
            FileToolkit(sandbox),
            PreviewToolkit(),
            MessageToolkit(),
            mcp_tool
        ]
        
        # Only add search tool when search_engine is not None
        if search_engine:
            tools.append(SearchToolkit(search_engine))

        runtime_prompt = build_runtime_environment_prompt(sandbox)

        # Planner receives only a compact capability overview; the executor
        # receives the invocable schemas themselves.
        self.planner = PlannerAgent(
            agent_id=self._agent_id,
            agent_repository=self._repository,
            llm=self._llm,
            capability_toolkits=tools,
            runtime_prompt=runtime_prompt,
        )
        logger.debug(f"Created planner agent for Agent {self._agent_id}")
            
        self.executor = ExecutionAgent(
            agent_id=self._agent_id,
            agent_repository=self._repository,
            llm=self._llm,
            tools=tools,
            runtime_prompt=runtime_prompt,
        )
        logger.debug(f"Created execution agent for Agent {self._agent_id}")

    async def _apply_project_instruction(self, project_id: Optional[str]) -> None:
        instruction: Optional[str] = None
        project_repository = getattr(self, "_project_repository", None)
        if project_id and project_repository:
            project = await project_repository.find_by_id(project_id)
            if project and project.instruction:
                instruction = project.instruction
        for agent in (self.planner, self.executor):
            set_instruction = getattr(agent, "set_project_instruction", None)
            if callable(set_instruction):
                set_instruction(instruction)
            sync_prompt = getattr(agent, "sync_system_prompt", None)
            if callable(sync_prompt) and hasattr(agent, "memory"):
                await sync_prompt()

    async def run(
        self,
        message: Message,
        resumes_waiting: Optional[bool] = None,
    ) -> AsyncGenerator[BaseEvent, None]:

        # TODO: move to task runner
        session = await self._session_repository.find_by_id(self._session_id)
        if not session:
            raise ValueError(f"Session {self._session_id} not found")

        await self._apply_project_instruction(
            getattr(session, "project_id", None)
        )

        if resumes_waiting is not None:
            # Durable workers receive the intent captured at acceptance time.
            # Do not infer it from Session.status: the durable RUNNING
            # projection is written immediately after the execution claim.
            if resumes_waiting:
                logger.debug(
                    "Session %s is resuming a persisted WAITING turn",
                    self._session_id,
                )
                await self.executor.roll_back(message)
                await self.planner.roll_back(message)
                self.status = AgentStatus.EXECUTING
            else:
                self.status = AgentStatus.PLANNING
        else:
            # Compatibility for legacy/non-durable callers that do not carry
            # an explicit acceptance-time resume decision.
            if session.status != SessionStatus.PENDING:
                logger.debug(f"Session {self._session_id} is not in PENDING status, rolling back")
                await self.executor.roll_back(message)
                await self.planner.roll_back(message)

            if session.status == SessionStatus.RUNNING:
                logger.debug(f"Session {self._session_id} is in RUNNING status")
                self.status = AgentStatus.PLANNING

            if session.status == SessionStatus.WAITING:
                logger.debug(f"Session {self._session_id} is in WAITING status")
                self.status = AgentStatus.EXECUTING

        await self._session_repository.update_status(self._session_id, SessionStatus.RUNNING)  
        self.plan = session.get_last_plan()
        zero_step_requires_summary = False

        logger.info(
            "Agent %s started processing message", self._agent_id
        )
        step = None
        while True:
            if self.status == AgentStatus.IDLE:
                logger.info(f"Agent {self._agent_id} state changed from {AgentStatus.IDLE} to {AgentStatus.PLANNING}")
                self.status = AgentStatus.PLANNING
            elif self.status == AgentStatus.PLANNING:
                # Create plan
                logger.info(f"Agent {self._agent_id} started creating plan")
                plan_created = False
                planning_error_emitted = False
                async for event in self.planner.create_plan(message):
                    if isinstance(event, PlanEvent) and event.status == PlanStatus.CREATED:
                        plan_created = True
                        self.plan = event.plan
                        logger.info(f"Agent {self._agent_id} created plan successfully with {len(event.plan.steps)} steps")
                        if event.plan.title and event.plan.title.strip():
                            yield TitleEvent(title=event.plan.title)
                    elif isinstance(event, ErrorEvent):
                        planning_error_emitted = True
                    yield event
                if not plan_created or self.plan is None:
                    logger.warning(f"Agent {self._agent_id} failed to create a plan")
                    if not planning_error_emitted:
                        yield ErrorEvent(error="Failed to create a plan.")
                    self.status = AgentStatus.IDLE
                    yield DoneEvent()
                    return
                logger.info(f"Agent {self._agent_id} state changed from {AgentStatus.PLANNING} to {AgentStatus.EXECUTING}")
                self.status = AgentStatus.EXECUTING
                if len(self.plan.steps) == 0:
                    logger.info(f"Agent {self._agent_id} created plan successfully with no steps")
                    # PlanOutput.message is an acknowledgement, not a final
                    # answer. Always let the executor produce deliver_result
                    # with the original request and plan context explicitly in
                    # view, including for simple or infeasible zero-step plans.
                    zero_step_requires_summary = True
                    self.status = AgentStatus.SUMMARIZING
                    
            elif self.status == AgentStatus.EXECUTING:
                # Execute plan
                self.plan.status = ExecutionStatus.RUNNING
                step = self.plan.get_next_step()
                if not step:
                    logger.info(f"Agent {self._agent_id} has no more steps, state changed from {AgentStatus.EXECUTING} to {AgentStatus.COMPLETED}")
                    self.status = AgentStatus.SUMMARIZING
                    continue
                # Execute step
                logger.info(
                    "Agent %s started executing step %s",
                    self._agent_id,
                    step.id,
                )
                async for event in self.executor.execute_step(self.plan, step, message):
                    yield event
                logger.info(f"Agent {self._agent_id} completed step {step.id}, state changed from {AgentStatus.EXECUTING} to {AgentStatus.UPDATING}")
                await self.executor.compact_memory()
                logger.debug(f"Agent {self._agent_id} compacted memory")
                self.status = AgentStatus.UPDATING
            elif self.status == AgentStatus.UPDATING:
                # Update plan
                logger.info(f"Agent {self._agent_id} started updating plan")
                async for event in self.planner.update_plan(self.plan, step):
                    if isinstance(event, ErrorEvent):
                        # Updating is advisory recovery between executable
                        # steps. The existing plan remains usable, while an
                        # ErrorEvent is terminal to SSE/durable/frontend
                        # consumers. Keep this failure internal and continue
                        # the remaining plan instead of ending the client
                        # stream before the worker's eventual result.
                        logger.warning(
                            "Agent %s could not update plan after step %s; "
                            "continuing the existing plan",
                            self._agent_id,
                            getattr(step, "id", "unknown"),
                        )
                        continue
                    yield event
                logger.info(f"Agent {self._agent_id} plan update completed, state changed from {AgentStatus.UPDATING} to {AgentStatus.EXECUTING}")
                self.status = AgentStatus.EXECUTING
            elif self.status == AgentStatus.SUMMARIZING:
                # Conclusion
                logger.info(f"Agent {self._agent_id} started summarizing")
                visible_summary_emitted = False
                async for event in self.executor.summarize(self.plan, message):
                    if isinstance(event, ErrorEvent) or (
                        isinstance(event, MessageEvent) and bool(event.message.strip())
                    ):
                        visible_summary_emitted = True
                    yield event
                if zero_step_requires_summary and not visible_summary_emitted:
                    yield ErrorEvent(
                        error="The agent completed without producing a visible response."
                    )
                logger.info(f"Agent {self._agent_id} summarizing completed, state changed from {AgentStatus.SUMMARIZING} to {AgentStatus.COMPLETED}")
                self.status = AgentStatus.COMPLETED
            elif self.status == AgentStatus.COMPLETED:
                self.plan.status = ExecutionStatus.COMPLETED
                logger.info(f"Agent {self._agent_id} plan has been completed")
                yield PlanEvent(status=PlanStatus.COMPLETED, plan=self.plan)
                self.status = AgentStatus.IDLE
                break
        yield DoneEvent()
        
        logger.info(f"Agent {self._agent_id} message processing completed")
    
    def is_done(self) -> bool:
        return self.status == AgentStatus.IDLE
