"""Break each load-bearing rule in turn and confirm the suite notices."""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent

MUTATIONS = [
    (
        "merge needs all inputs live",
        "execution.py",
        "    if node_type.kind is NodeKind.MERGE:\n        return False\n",
        "",
    ),
    (
        "plain node needs only one live input",
        "execution.py",
        "    return any(edge not in live for edge in required)\n",
        "    return False\n",
    ),
    (
        "dead optional edge skips the node",
        "execution.py",
        "required = [e for e in inbound if e.target_port not in node_type.optional_input_ports]",
        "required = list(inbound)",
    ),
    (
        "all-dead optional inputs still run",
        "execution.py",
        "    if not live:\n        return True\n",
        "",
    ),
    (
        "skipped node writes its ports",
        "execution.py",
        'return result("skipped", {})',
        'return result("skipped", dict.fromkeys(node_type.output_ports))',
    ),
    (
        "skipped nodes use up steps",
        "execution.py",
        "        live = live_edges(inbound, records)\n",
        "        live = live_edges(inbound, records)\n        runtime.context.steps_used += 1\n",
    ),
    (
        "max steps never enforced",
        "execution.py",
        "if context.steps_used >= context.limits.max_steps:",
        "if False:",
    ),
    (
        "failure after tokens ignored",
        "execution.py",
        "    if has_sent_tokens:\n        return FailTheRun()\n",
        "",
    ),
    (
        "any model tokens block retry",
        "execution.py",
        "has_sent_tokens=is_stream_source and bool(tokens)",
        "has_sent_tokens=bool(tokens)",
    ),
    (
        "interrupt treated as failure",
        "execution.py",
        "            except GraphInterrupt:\n",
        "            except ZeroDivisionError:\n",
    ),
    (
        "undeclared node may park",
        "execution.py",
        "                if not node_type.can_park(node.settings):\n",
        "                if False:\n",
    ),
    (
        "cancel ignored between retries",
        "execution.py",
        "                    if runtime.drain_requested:\n                        return await stop_cancelled(attempt)\n",
        "",
    ),
    (
        "handler cancel not honoured",
        "execution.py",
        "            except NodeCancelled:\n                return await stop_cancelled(attempt)\n",
        "",
    ),
    ("no backoff growth", "execution.py", "backoff * 2 ** (attempt - 1)", "backoff"),
    (
        "continue_with_null writes nothing",
        "execution.py",
        'return result("failed", dict.fromkeys(node_type.output_ports))',
        'return result("failed", {})',
    ),
    (
        "all stream handlers reach the client",
        "execution.py",
        "            if is_stream_source:\n                await adispatch_custom_event(TOKEN",
        "            if True:\n                await adispatch_custom_event(TOKEN",
    ),
    (
        "empty tokens count as sent",
        "execution.py",
        "            if not token:\n                continue\n",
        "",
    ),
    (
        "handler outputs unchecked",
        "execution.py",
        "    check_outputs(node_type, outputs)\n    return outputs\n",
        "    return outputs\n",
    ),
    (
        "non-JSON output accepted",
        "execution.py",
        "        json.dumps(dict(outputs), allow_nan=False)\n",
        "        pass\n",
    ),
    (
        "no timeout enforced",
        "runner.py",
        "run_timeout = asyncio.timeout(context.limits.timeout_seconds)",
        "run_timeout = asyncio.timeout(None)",
    ),
    (
        "in-flight steps left open",
        "runner.py",
        "            emit(StepEvent(node_id, node_type, StepStatus.CANCELLED))\n",
        "            pass\n",
    ),
    (
        "parked steps left open",
        "runner.py",
        "_OPEN_STATUSES = (StepStatus.RUNNING, StepStatus.RETRYING, StepStatus.PARKED)",
        "_OPEN_STATUSES = (StepStatus.RUNNING, StepStatus.RETRYING)",
    ),
    (
        "no final-text token",
        "runner.py",
        "    elif status is RunStatus.COMPLETED and isinstance(progress.output, str):\n",
        "    elif False:\n",
    ),
    (
        "rewritten output goes unnoticed",
        "runner.py",
        'if "".join(progress.sent_text) != progress.output:',
        "if False:",
    ),
    (
        "parked reported as completed",
        "runner.py",
        "        return RunStatus.PARKED\n",
        "        return RunStatus.COMPLETED\n",
    ),
    (
        "cancel during a park is lost",
        "runner.py",
        "        if control.drain_requested:\n            progress.parked = ()\n            return RunStatus.CANCELLED\n",
        "",
    ),
    (
        "cancelled last step reads as completed",
        "runner.py",
        "return RunStatus.CANCELLED if progress.has_cancelled_step else RunStatus.COMPLETED",
        "return RunStatus.COMPLETED",
    ),
    (
        "resume forgets used steps",
        "runner.py",
        "return _RunEntry(graph_input, _steps_used(snapshot), parked)",
        "return _RunEntry(graph_input, 0, parked)",
    ),
    (
        "run id reuse allowed",
        "runner.py",
        "if workflow.has_checkpointer and (await workflow.graph.aget_state(config)).values:",
        "if False:",
    ),
    (
        "resume target unchecked",
        "runner.py",
        "if not mode.values or not set(mode.values) <= parked_ids:",
        "if False:",
    ),
    ("recover state unchecked", "runner.py", "    if not snapshot.next:\n", "    if False:\n"),
    (
        "unexpected errors escape",
        "runner.py",
        "    except Exception as error:\n        # The run boundary",
        "    except ZeroDivisionError as error:\n        # The run boundary",
    ),
    (
        "closing the stream leaves the run going",
        "runner.py",
        "    finally:\n        pump.cancel()\n        with suppress(asyncio.CancelledError):\n            await pump\n",
        "    finally:\n        pass\n",
    ),
    (
        "recursion limit left tiny",
        "runner.py",
        '"recursion_limit": len(workflow.spec.nodes) + 1,',
        '"recursion_limit": 25,',
    ),
    (
        "exclusivity check removed",
        "compiler.py",
        "if first_id != second_id and not _are_mutually_exclusive(first, second):",
        "if False:",
    ),
    (
        "merge choices unioned, not intersected",
        "compiler.py",
        "required[node_id] = frozenset.intersection(*choices(edges))",
        "required[node_id] = frozenset.union(*choices(edges))",
    ),
    (
        "plain joins instead of a barrier",
        "compiler.py",
        "            builder.add_edge(predecessors, node.id)\n",
        "            for predecessor in predecessors:\n                builder.add_edge(predecessor, node.id)\n",
    ),
    (
        "transparency ignored in walk",
        "compiler.py",
        "if registry[source.type].is_stream_transparent(source.settings):",
        "if registry[source.type].kind is NodeKind.MERGE:",
    ),
    (
        "walk follows every Output port",
        "compiler.py",
        "pending = [edge for edge in inbound[output_node_id] if edge.target_port == TEXT_PORT]",
        "pending = list(inbound[output_node_id])",
    ),
    (
        "null allowed after a stream source",
        "compiler.py",
        '            if isinstance(node.on_failure, ContinueWithNull):\n                raise GraphInvalid(node.id, "continue_with_null would',
        '            if False:\n                raise GraphInvalid(node.id, "continue_with_null would',
    ),
    (
        "retry allowed on parking node",
        "compiler.py",
        "if node_type.can_park(node.settings) and isinstance(node.on_failure, Retry):",
        "if False:",
    ),
    (
        "parking without a checkpointer allowed",
        "compiler.py",
        "if registry[node.type].can_park(node.settings) and checkpointer is None:",
        "if False:",
    ),
    ("unconnected required port allowed", "compiler.py", "    if missing:\n", "    if False:\n"),
    (
        "optional edges count for exclusivity",
        "compiler.py",
        "required[node_id] = frozenset.union(*choices(must_be_live))",
        "required[node_id] = frozenset.union(*choices(edges))",
    ),
    (
        "checkpointed runs stream live",
        "compiler.py",
        "        if checkpointer is not None\n        else find_stream_sources",
        "        if False\n        else find_stream_sources",
    ),
    (
        "outcome read from this stream only",
        "runner.py",
        "        _load_outcome(await workflow.graph.aget_state(config), workflow, progress)\n",
        "        progress.parked = _parked_nodes(await workflow.graph.aget_state(config))\n",
    ),
    ("parked steps not reopened on entry", "runner.py", "dict(entry.open_steps),", "{},"),
    (
        "store timeout reads as run timeout",
        "runner.py",
        "if not run_timeout.expired():",
        "if False:",
    ),
    ("NaN accepted", "execution.py", ", allow_nan=False", ""),
    (
        "output from an earlier stream dropped",
        "runner.py",
        "    if workflow.has_checkpointer and status is not RunStatus.COMPLETED and has_readable_state:\n",
        "    if False:\n",
    ),
    (
        "retries uncapped",
        "spec.py",
        "max_retries: int = Field(ge=1, le=MAX_RETRIES)",
        "max_retries: int = Field(ge=1)",
    ),
]


def run_tests() -> tuple[bool, str]:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    failed = [line for line in done.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))]
    return done.returncode != 0, (failed[0][:95] if failed else "")


survivors = []
for name, filename, old, new in MUTATIONS:
    path = ROOT / "runner_spike" / filename
    original = path.read_bytes()
    text = original.decode()
    normal = text.replace("\r\n", "\n")
    if old not in normal:
        print(f"NOT APPLIED  {name}")
        survivors.append(name)
        continue
    path.write_bytes(normal.replace(old, new, 1).encode())
    try:
        caught, first = run_tests()
    except subprocess.TimeoutExpired:
        caught, first = True, "(suite hung: caught by timeout)"
    finally:
        path.write_bytes(original)
    print(f"{'caught  ' if caught else 'SURVIVED'}  {name:42} {first}")
    if not caught:
        survivors.append(name)

print("\nsurvivors:", survivors or "none")
sys.exit(1 if survivors else 0)
