"""Policy navigator (w-9-2-planner P3): read-only LangChain v1 + Bedrock
agent over the policy corpus retriever, with a citation-validation safety
net. No graph store, no persistence, no state-changing tools — see
runtime.py's module docstring for the full boundary.
"""
from .contracts import CitedSource, PolicyNavigatorResult
from .runtime import PROMPT_VERSION, SYSTEM_PROMPT, ProviderNotConfigured, run_policy_navigator
from .scope import scope_for_role
from .tool import TOOL_NAME, build_policy_tool

__all__ = [
    "CitedSource", "PolicyNavigatorResult", "PROMPT_VERSION", "SYSTEM_PROMPT", "ProviderNotConfigured",
    "run_policy_navigator", "scope_for_role", "TOOL_NAME", "build_policy_tool",
]
