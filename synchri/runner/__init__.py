"""Single-terminal operation: drive several agents' turns from one process."""

from .agent_command import AgentCommand, AgentResult, Directives, parse_directives
from .conductor import Conductor, ConductorReport
from .managed import ManagedRunnerRegistry

__all__ = [
    "AgentCommand",
    "AgentResult",
    "Conductor",
    "ConductorReport",
    "ManagedRunnerRegistry",
    "Directives",
    "parse_directives",
]
