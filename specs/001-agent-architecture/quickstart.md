# Quickstart Validation

This guide validates the architecture without requiring production QQ credentials.

## Prerequisites

1. Install Python dependencies from `requirements-mvp.txt`.
2. Install frontend dependencies with `npm install`.
3. Configure a test model or deterministic fake LLM and set an isolated workspace/database path.

## Web smoke path

1. Build the desktop chat (`npm run build:chat`) or run the development server.
2. Start `python -m uvicorn backend.app:app --host 127.0.0.1 --port 2236`.
3. Connect to `/ws`, send `session.attach`, then `message.send` with a fixed `request_id` and `turn_id`.
4. Assert `turn.started`, any tool frames, `answer.delta`, `message.final`, and `turn.output.completed` arrive with matching session/turn IDs.
5. Query `/api/chat/sessions/{session}/messages` and assert exactly one user message and one assistant message.

## QQ adapter path

1. Feed a private OneBot fixture to the adapter and assert one canonical turn and one outbound reply.
2. Feed a group fixture without a mention and assert zero turns.
3. Feed a group fixture with one or multiple mentions and assert one turn whose text excludes the mention.
4. Replay the same external message ID and assert no second turn or tool invocation.

## Memory path

1. Seed an isolated canonical database with fixed historical turns and dense/BM25 scores.
2. Run recall and assert the exact `_tail_surprisal` values for empty, all-zero, repeated, negative, and ordinary arrays.
3. Assert recall provenance fields and that prompt token cost never exceeds the configured remaining budget.
4. Disable embeddings and verify the turn completes with a `memory_degraded` event and deterministic lexical fallback.
5. Delete the derived memory database, restart, rebuild, and assert canonical messages remain unchanged.

## Tool and safety path

1. Request an allowlisted file or shell operation and assert schema validation, timeout, bounded output, and tool lifecycle events.
2. Request `spawn` and `spawn_manage`; assert denial/unsupported status, no child execution, and an audit event.
3. Put a fake API key in configuration and assert it is absent from settings responses, logs, tool previews, and errors.

## Recommended commands

```powershell
pytest -q test/backend
npm run typecheck
npm run lint
npm run build:chat
```

Run only against isolated test databases and fixture workspaces; never reuse the developer's normal `sessions.db`.
