# SPDX-License-Identifier: Apache-2.0
"""
State models for TheMatrix Simulation Studio.

All state models carry type and schema_version fields for forward compatibility
and migration support.
"""

from typing import Any, Dict, List, Optional
import uuid
from pydantic import BaseModel, Field


class MemoryItem(BaseModel):
    """A single memory item in an agent's memory stream."""
    type: str = Field(default="MemoryItem", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version for migration")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="Stable memory id (for memory_refs)")
    timestamp: int = Field(description="Unix timestamp")
    content: str = Field(description="Memory content")
    importance: Optional[float] = Field(default=None, description="Importance score 0-1")
    tags: List[str] = Field(default_factory=list, description="Memory tags")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class AgentState(BaseModel):
    """Complete state for a single agent."""
    type: str = Field(default="AgentState", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version for migration")
    name: str = Field(description="Agent name")
    persona: str = Field(description="Agent persona/system message")
    memory_stream: List[MemoryItem] = Field(default_factory=list, description="Agent's memory")
    goals: List[str] = Field(default_factory=list, description="Current goals")
    relationships: Dict[str, str] = Field(default_factory=dict, description="Relationships to other agents")
    conversation_history: List[Dict[str, Any]] = Field(default_factory=list, description="Recent conversation context")
    total_tokens_in: int = Field(default=0, description="Total input tokens consumed")
    total_tokens_out: int = Field(default=0, description="Total output tokens generated")
    total_cost_usd: float = Field(default=0.0, description="Total cost in USD")
    portrait: Optional[str] = Field(default=None, description="Base64 encoded avatar image")


class CognitionConfig(BaseModel):
    """Phase 2c cognition flags (per-run, read from ``config['cognition']``).

    All flags default to the pre-2c behavior: when ``enabled`` is False the
    engine's generation path is byte-for-byte identical to Phase 2b (no
    structured output, no new events, no extra token cost). ``enabled`` is the
    master switch; the sub-flags only take effect when it is True.
    """
    type: str = Field(default="CognitionConfig", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version")
    enabled: bool = Field(default=False, description="Master switch for cognition")
    memory: bool = Field(default=True, description="Form + retrieve agent memories (on when enabled)")
    reflection_every: int = Field(
        default=4, ge=0,
        description="Reflect every N turns (0 disables); ON by default when enabled",
    )
    goals_dynamic: bool = Field(default=False, description="Allow agents to update their own goals")
    relationships: bool = Field(default=False, description="Track per-agent stance toward others")
    retrieval_k: int = Field(default=5, ge=0, description="Memories injected into each turn's prompt")

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "CognitionConfig":
        """Parse from a run ``config`` dict. Missing/invalid -> all-off default.

        Accepts ``config['cognition']`` as a dict; anything else yields the
        disabled default so legacy/plain configs behave exactly as before.
        """
        raw = (config or {}).get("cognition")
        if not isinstance(raw, dict):
            return cls()
        return cls(**{k: v for k, v in raw.items() if k in cls.model_fields})


class SimSnapshot(BaseModel):
    """Complete simulation state snapshot."""
    type: str = Field(default="SimSnapshot", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version for migration")
    run_id: str = Field(description="Unique run identifier")
    turn: int = Field(description="Turn number of this snapshot")
    topic: str = Field(description="Simulation topic")
    agents: Dict[str, AgentState] = Field(description="Agent states keyed by name")
    conversation: List[Dict[str, Any]] = Field(default_factory=list, description="Full conversation transcript")
    status: str = Field(description="Simulation status: pending|running|complete|failed")
    created_at: int = Field(description="Unix timestamp of snapshot creation")
    completed_at: Optional[int] = Field(default=None, description="Unix timestamp of completion")
    total_turns: int = Field(description="Total turns executed")
    error_message: Optional[str] = Field(default=None, description="Error message if failed")
