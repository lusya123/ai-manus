from typing import Optional, List
from datetime import datetime, UTC
from app.domain.models.agent import Agent
from app.domain.models.memory import Memory
from app.domain.repositories.agent_repository import AgentRepository
from app.infrastructure.models.documents import AgentDocument
from app.infrastructure.models.memory_serialization import deserialize_memory, serialize_memory
import logging


logger = logging.getLogger(__name__)

class MongoAgentRepository(AgentRepository):
    """MongoDB implementation of AgentRepository"""

    async def save(self, agent: Agent) -> None:
        """Save or update an agent"""
        mongo_agent = await AgentDocument.find_one(
            AgentDocument.agent_id == agent.id
        )
        
        if not mongo_agent:
            mongo_agent = AgentDocument.from_domain(agent)
            await mongo_agent.save()
            return

        # Build the replacement through AgentDocument's credential serializer;
        # BaseDocument.update_from_domain() would otherwise copy api_key into a
        # plaintext MongoDB field.
        replacement = AgentDocument.from_domain(agent)
        for field, value in replacement.model_dump(exclude={"id"}).items():
            setattr(mongo_agent, field, value)
        await mongo_agent.save()

    async def find_by_id(self, agent_id: str) -> Optional[Agent]:
        """Find an agent by its ID"""
        mongo_agent = await AgentDocument.find_one(
            AgentDocument.agent_id == agent_id
        )
        if not mongo_agent:
            return None
        agent = mongo_agent.to_domain()
        if mongo_agent.api_key:
            # Opportunistically migrate both historical BYOK credentials and
            # duplicated system keys away from the legacy plaintext field.
            replacement = AgentDocument.from_domain(agent)
            mongo_agent.api_key = None
            mongo_agent.api_key_encrypted = replacement.api_key_encrypted
            mongo_agent.is_byok = agent.is_byok
            await mongo_agent.save()
        return agent

    async def delete(self, agent_id: str) -> None:
        """Delete an agent and any encrypted per-session credential it owns."""
        mongo_agent = await AgentDocument.find_one(
            AgentDocument.agent_id == agent_id
        )
        if mongo_agent:
            await mongo_agent.delete()

    async def add_memory(self, agent_id: str,
                          name: str,
                          memory: Memory) -> None:
        """Add or update a memory for an agent"""
        result = await AgentDocument.find_one(
            AgentDocument.agent_id == agent_id
        ).update(
            {"$set": {f"memories.{name}": serialize_memory(memory), "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Agent {agent_id} not found")

    async def get_memory(self, agent_id: str, name: str) -> Memory:
        """Get memory by name from agent, create if not exists"""
        mongo_agent = await AgentDocument.find_one(
            AgentDocument.agent_id == agent_id
        )
        if not mongo_agent:
            raise ValueError(f"Agent {agent_id} not found")
        return deserialize_memory(mongo_agent.memories.get(name))
    
    async def save_memory(self, agent_id: str, name: str, memory: Memory) -> None:
        """Update the messages of a memory"""
        result = await AgentDocument.find_one(
            AgentDocument.agent_id == agent_id
        ).update(
            {"$set": {f"memories.{name}": serialize_memory(memory), "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Agent {agent_id} not found")
