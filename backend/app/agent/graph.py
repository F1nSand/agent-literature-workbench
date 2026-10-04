from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph


class AgentState(TypedDict, total=False):
    run_id: str
    messages: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    tool_calls_used: int
    model_steps_used: int
    force_final: bool
    finish_reason: str
    assistant_content: str
    answer: str
    citation_rows: list[dict[str, Any]]
    finalized: bool


Node = Callable[[AgentState], dict[str, Any]]


def build_research_graph(
    checkpointer: BaseCheckpointSaver,
    *,
    model_step: Node,
    execute_tool: Node,
    finalize: Node,
) -> Any:
    graph = StateGraph(AgentState)
    graph.add_node("model", model_step)
    graph.add_node("tool", execute_tool)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", _after_model, {"tool": "tool", "finalize": "finalize"})
    graph.add_conditional_edges("tool", _after_tool, {"tool": "tool", "model": "model", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def _after_model(state: AgentState) -> str:
    if state.get("pending_tool_calls"):
        return "tool"
    return "finalize"


def _after_tool(state: AgentState) -> str:
    if state.get("finish_reason"):
        return "finalize"
    if state.get("pending_tool_calls"):
        return "tool"
    return "model"
