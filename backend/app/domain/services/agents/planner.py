import logging
from typing import AsyncGenerator, List, Optional

from app.domain.external.llm import LLM
from app.domain.models.agent_output import PlanOutput, PlanUpdateOutput
from app.domain.models.event import BaseEvent, PlanEvent, PlanStatus
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.services.agents.base import BaseAgent, StructuredOutputEvent
from app.domain.services.prompts.planner import (
    CREATE_PLAN_PROMPT,
    PLANNER_ROLE_PROMPT,
    UPDATE_PLAN_PROMPT,
)
from app.domain.services.prompts.system import build_system_prompt
from app.domain.services.tools.base import (
    BaseToolkit,
    OutputTool,
    describe_toolkits,
)


logger = logging.getLogger(__name__)

CREATE_PLAN_TOOL = OutputTool(
    name="create_plan",
    description="Submit the plan for the user's request.",
    schema=PlanOutput,
)

UPDATE_PLAN_TOOL = OutputTool(
    name="update_plan",
    description="Submit the re-planned remaining steps.",
    schema=PlanUpdateOutput,
)


class PlannerAgent(BaseAgent):
    """Plan with a compact capability overview and native output calls."""

    name: str = "planner"
    tool_choice: Optional[str] = "required"

    def __init__(
        self,
        agent_id: str,
        agent_repository: AgentRepository,
        llm: LLM,
        capability_toolkits: Optional[List[BaseToolkit]] = None,
        runtime_prompt: str = "",
    ):
        # Planner receives no work schemas. It can only submit its output tool,
        # avoiding the context cost and accidental execution of executor tools.
        super().__init__(
            agent_id=agent_id,
            agent_repository=agent_repository,
            llm=llm,
            tools=[],
        )
        self._capability_toolkits = capability_toolkits or []
        self._runtime_prompt = runtime_prompt

    def build_system_prompt(self) -> str:
        # Render lazily so MCP tools discovered after flow construction appear
        # in the overview and stale persisted prompts are refreshed.
        return build_system_prompt(
            toolkits=[],
            runtime_prompt=self._runtime_prompt,
            role_prompt=PLANNER_ROLE_PROMPT.format(
                capabilities=describe_toolkits(self._capability_toolkits)
            ),
        )

    async def create_plan(
        self, message: Message
    ) -> AsyncGenerator[BaseEvent, None]:
        request = CREATE_PLAN_PROMPT.format(
            message=message.message,
            attachments="\n".join(message.attachments),
        )
        async for event in self.execute(request, output_tool=CREATE_PLAN_TOOL):
            if isinstance(event, StructuredOutputEvent):
                output: PlanOutput = event.output
                logger.info("Planner created plan: steps=%d", len(output.steps))
                yield PlanEvent(
                    status=PlanStatus.CREATED,
                    plan=Plan.model_validate(output.model_dump()),
                )
            else:
                yield event

    async def update_plan(
        self, plan: Plan, step: Step
    ) -> AsyncGenerator[BaseEvent, None]:
        request = UPDATE_PLAN_PROMPT.format(
            plan=plan.dump_json(),
            step=step.model_dump_json(),
        )
        async for event in self.execute(request, output_tool=UPDATE_PLAN_TOOL):
            if isinstance(event, StructuredOutputEvent):
                output: PlanUpdateOutput = event.output
                new_steps = [
                    Step.model_validate(item.model_dump()) for item in output.steps
                ]

                first_pending_index = next(
                    (
                        index
                        for index, existing in enumerate(plan.steps)
                        if not existing.is_done()
                    ),
                    None,
                )
                # When the just-finished step was the final existing step,
                # every step is already terminal and there is no pending
                # index. Recovery/follow-up steps returned by the planner must
                # still be appended instead of being silently discarded.
                replace_from = (
                    first_pending_index
                    if first_pending_index is not None
                    else len(plan.steps)
                )
                plan.steps = plan.steps[:replace_from] + new_steps

                logger.debug(
                    "Planner updated plan: remaining_steps=%d", len(new_steps)
                )
                yield PlanEvent(status=PlanStatus.UPDATED, plan=plan)
            else:
                yield event
