# Research: Akashic Agent Core Architecture

## Decision 1: Extend the existing runtime boundaries

**Decision**: Keep FastAPI, MessageBus, SessionStore, AgentOrchestrator, ContextManager, model runtime, and Akasha memory as separate boundaries. Add a channel adapter boundary instead of creating a second QQ-specific Agent.

**Evidence**: `backend/app.py` already wires WebSocket input through MessageBus, session persistence, memory recall, Agent execution, and final events. `backend-contract.md` defines the Web compatibility surface.

**Alternatives considered**:

- A separate QQ Agent would duplicate prompt, tool, and memory behavior and make cross-channel parity untestable.
- Replacing the existing Web protocol would break the current frontend and violate the constitution.

## Decision 2: Canonical facts in sessions.db; memory is derived

**Decision**: Treat sessions, turns, and messages as authoritative. Keep `memory/akasha.db` rebuildable and commit it only after the assistant message is durable.

**Evidence**: `backend/memory/akasha.py` already documents this ownership and performs read-before-write recall.

**Alternatives considered**:

- Making the memory sidecar authoritative would risk data loss and make index repair impossible.

## Decision 3: Copy the source `_tail_surprisal` exactly

**Decision**: Use the implementation from `akashic-agent-main/plugins/akasha/domain/features.py:792`:

```python
if scores.size == 0 or not np.any(scores != 0.0):
    return np.zeros_like(scores)
ordered = np.sort(scores, kind="stable")
counts = scores.size - np.searchsorted(ordered, scores, side="left")
return -np.log(counts / scores.size)
```

Apply it independently to dense, BM25, context-dense, and context-BM25 arrays before their lane combination. RRF remains confined to the independent `memory2` retriever route.

**Alternatives considered**:

- Min-max, z-score, or an RRF-first pipeline would not reproduce source behavior.

## Decision 4: One main Agent, no child Agent capability

**Decision**: Remove `spawn` and `spawn_manage` from the effective tool schema and return an auditable unsupported/denied result if requested. Keep a single bounded ReAct loop.

**Evidence**: Current `backend/agent_orchestrator.py` exposes `SpawnTool`; this conflicts with the accepted product decision and must be changed in implementation tasks.

**Alternatives considered**:

- Keeping hidden child execution would violate the permission boundary and make resource use unpredictable.

## Decision 5: QQ adapter contract before production transport

**Decision**: Normalize OneBot-style private/group events into `ChannelEnvelope`; process private messages directly and group messages only after mention validation. Start with deterministic test doubles; production gateway credentials and deployment wiring are separate.

**Alternatives considered**:

- Binding Agent logic directly to a QQ SDK would couple protocol details to core runtime and make Web/QQ parity harder to verify.

## Decision 6: No backend queue beyond admission control

**Decision**: Enforce one unfinished turn per session with idempotency and a busy response. Do not add a general session queue in this milestone because the Web UI already prevents normal concurrent sends.

**Risk**: Non-Web clients may still retry aggressively; idempotency and explicit busy errors are therefore mandatory.
