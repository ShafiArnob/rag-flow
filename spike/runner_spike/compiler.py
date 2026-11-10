"""GraphSpec to LangGraph `StateGraph`, plus the static facts the runner needs."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from itertools import combinations
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from runner_spike.events import RunContext, RunState
from runner_spike.execution import make_step
from runner_spike.nodes import TEXT_PORT, NodeKind, NodeType
from runner_spike.spec import ContinueWithNull, EdgeSpec, GraphSpec, NodeSpec, Retry

BranchChoice = tuple[str, str]


class GraphInvalid(Exception):
    def __init__(self, node_id: str | None, message: str) -> None:
        super().__init__(f"{node_id or 'graph'}: {message}")
        self.node_id = node_id


@dataclass(frozen=True)
class CompiledWorkflow:
    spec: GraphSpec
    graph: CompiledStateGraph[RunState, RunContext, RunState, RunState]
    output_node_id: str | None
    stream_sources: frozenset[str]
    has_checkpointer: bool


def _inbound_edges(spec: GraphSpec) -> dict[str, list[EdgeSpec]]:
    inbound: dict[str, list[EdgeSpec]] = defaultdict(list)
    for edge in spec.edges:
        inbound[edge.target].append(edge)
    return inbound


def _topological_order(spec: GraphSpec) -> list[str]:
    predecessors: dict[str, set[str]] = {node.id: set() for node in spec.nodes}
    for edge in spec.edges:
        predecessors[edge.target].add(edge.source)
    try:
        return list(TopologicalSorter(predecessors).static_order())
    except CycleError as error:
        raise GraphInvalid(error.args[1][0], "node is part of a cycle") from error


def _validate_node(
    node: NodeSpec, node_type: NodeType, inbound: list[EdgeSpec], nodes: Mapping[str, NodeSpec]
) -> None:
    if node_type.kind is NodeKind.BRANCH and isinstance(node.on_failure, ContinueWithNull):
        raise GraphInvalid(node.id, "a branch node cannot continue_with_null: no port is taken")
    if node_type.can_park(node.settings) and isinstance(node.on_failure, Retry):
        raise GraphInvalid(node.id, "a parking node cannot retry: it would park a second time")
    ports = [edge.target_port for edge in inbound]
    missing = set(node_type.input_ports) - set(node_type.optional_input_ports) - set(ports)
    if missing:
        raise GraphInvalid(node.id, f"required input ports not connected: {sorted(missing)}")
    if set(ports) - set(node_type.input_ports):
        raise GraphInvalid(node.id, f"unknown input port in {sorted(set(ports))}")
    if node_type.kind is not NodeKind.MERGE and len(ports) != len(set(ports)):
        raise GraphInvalid(node.id, "only a Merge input port may take more than one edge")
    for edge in inbound:
        if edge.source not in nodes:
            raise GraphInvalid(node.id, f"edge from unknown node {edge.source}")


def _validate(spec: GraphSpec, registry: Mapping[str, NodeType]) -> None:
    nodes = {node.id: node for node in spec.nodes}
    if len(nodes) != len(spec.nodes):
        raise GraphInvalid(None, "node ids must be unique")
    for node in spec.nodes:
        if node.type not in registry:
            raise GraphInvalid(node.id, f"unknown node type {node.type}")
    for edge in spec.edges:
        if edge.target not in nodes:
            raise GraphInvalid(edge.source, f"edge to unknown node {edge.target}")
    inbound = _inbound_edges(spec)
    for node in spec.nodes:
        _validate_node(node, registry[node.type], inbound[node.id], nodes)
    for edge in spec.edges:
        output_ports = registry[nodes[edge.source].type].output_ports
        if edge.source_port not in output_ports:
            raise GraphInvalid(edge.source, f"unknown output port {edge.source_port}")
    outputs = [n.id for n in spec.nodes if registry[n.type].kind is NodeKind.OUTPUT]
    if len(outputs) > 1:
        raise GraphInvalid(outputs[1], "a graph has at most one Output node")


def _edge_choices(
    edge: EdgeSpec,
    required: Mapping[str, frozenset[BranchChoice]],
    nodes: Mapping[str, NodeSpec],
    registry: Mapping[str, NodeType],
) -> frozenset[BranchChoice]:
    if registry[nodes[edge.source].type].kind is NodeKind.BRANCH:
        return required[edge.source] | {(edge.source, edge.source_port)}
    return required[edge.source]


def required_branch_choices(
    spec: GraphSpec, registry: Mapping[str, NodeType]
) -> dict[str, frozenset[BranchChoice]]:
    """For each node, the (branch node, port) choices that must all hold for it to run."""
    nodes = {node.id: node for node in spec.nodes}
    inbound = _inbound_edges(spec)
    required: dict[str, frozenset[BranchChoice]] = {}
    for node_id in _topological_order(spec):
        node_type = registry[nodes[node_id].type]
        edges = inbound[node_id]
        must_be_live = [e for e in edges if e.target_port not in node_type.optional_input_ports]

        def choices(of: list[EdgeSpec]) -> list[frozenset[BranchChoice]]:
            return [_edge_choices(edge, required, nodes, registry) for edge in of]

        if not edges:
            required[node_id] = frozenset()
        elif node_type.kind is NodeKind.MERGE or not must_be_live:
            required[node_id] = frozenset.intersection(*choices(edges))
        else:
            required[node_id] = frozenset.union(*choices(must_be_live))
    return required


def _are_mutually_exclusive(
    first: frozenset[BranchChoice], second: frozenset[BranchChoice]
) -> bool:
    return any(
        branch == other_branch and port != other_port
        for branch, port in first
        for other_branch, other_port in second
    )


def find_stream_sources(
    spec: GraphSpec, registry: Mapping[str, NodeType], output_node_id: str | None
) -> frozenset[str]:
    """The model nodes whose tokens may go to the client live.

    Walks back from Output through stream-transparent nodes to every node that can supply the
    final text. Those suppliers must be pairwise mutually exclusive, or live tokens could
    interleave or disagree with the final output; then nothing streams and the runner sends the
    final text as one token. A branch node takes exactly one port, which is what makes the
    exclusivity check sound.
    """
    if output_node_id is None:
        return frozenset()
    nodes = {node.id: node for node in spec.nodes}
    inbound = _inbound_edges(spec)
    required = required_branch_choices(spec, registry)

    suppliers: list[tuple[str, frozenset[BranchChoice]]] = []
    after_source = [nodes[output_node_id]]
    pending = [edge for edge in inbound[output_node_id] if edge.target_port == TEXT_PORT]
    while pending:
        edge = pending.pop()
        source = nodes[edge.source]
        if registry[source.type].is_stream_transparent(source.settings):
            after_source.append(source)
            pending.extend(inbound[source.id])
        else:
            suppliers.append((source.id, _edge_choices(edge, required, nodes, registry)))

    for (first_id, first), (second_id, second) in combinations(suppliers, 2):
        if first_id != second_id and not _are_mutually_exclusive(first, second):
            return frozenset()
    sources = frozenset(
        node_id for node_id, _ in suppliers if registry[nodes[node_id].type].stream is not None
    )
    if sources:
        for node in after_source:
            if isinstance(node.on_failure, ContinueWithNull):
                raise GraphInvalid(node.id, "continue_with_null would contradict streamed tokens")
    return sources


def compile_workflow(
    spec: GraphSpec,
    registry: Mapping[str, NodeType],
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledWorkflow:
    _validate(spec, registry)
    _topological_order(spec)
    for node in spec.nodes:
        if registry[node.type].can_park(node.settings) and checkpointer is None:
            raise GraphInvalid(node.id, "a parking node needs a checkpointer")

    output_node_id = next(
        (node.id for node in spec.nodes if registry[node.type].kind is NodeKind.OUTPUT), None
    )
    # A checkpointed run can span several streams and repeat work, so tokens sent live could
    # not be kept consistent. It delivers its final text once, when it completes.
    stream_sources = (
        frozenset()
        if checkpointer is not None
        else find_stream_sources(spec, registry, output_node_id)
    )
    inbound = _inbound_edges(spec)
    has_successor = {edge.source for edge in spec.edges}

    builder: StateGraph[RunState, RunContext, RunState, RunState] = StateGraph(
        RunState, context_schema=RunContext
    )
    for node in spec.nodes:
        step = make_step(
            node,
            registry[node.type],
            inbound[node.id],
            is_stream_source=node.id in stream_sources,
        )
        builder.add_node(node.id, step)

    for node in spec.nodes:
        predecessors = list(dict.fromkeys(edge.source for edge in inbound[node.id]))
        if not predecessors:
            builder.add_edge(START, node.id)
        elif len(predecessors) == 1:
            builder.add_edge(predecessors[0], node.id)
        else:
            builder.add_edge(predecessors, node.id)
        if node.id not in has_successor:
            builder.add_edge(node.id, END)

    return CompiledWorkflow(
        spec=spec,
        graph=builder.compile(checkpointer=checkpointer),
        output_node_id=output_node_id,
        stream_sources=stream_sources,
        has_checkpointer=checkpointer is not None,
    )
