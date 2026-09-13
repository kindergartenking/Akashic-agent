# Web Compatibility Contract

The implementation MUST preserve the current `backend-contract.md` surface. This document records the architecture boundary; field additions must remain backward-compatible.

## HTTP

Keep the existing `/api/shell/state`, `/api/chat/sessions`, `/api/chat/sessions/{session}/messages`, `/api/chat/models`, upload/media, runtime, plugin UI, settings, and dashboard routes. Errors use `{ "detail": string | object }`; settings mutations require the existing CSRF header and revision checks.

## WebSocket `/ws`

Client frames:

- `session.attach`: `request_id`, `session_id`
- `message.send`: `request_id`, `session_id`, `text`, optional `media`, `reply_to_message_id`, model overrides
- `turn.stop`: `request_id`, `session_id`

Server frames retain these names and required fields:

- `session.created`
- `turn.started`
- `react.thinking.delta` (status-only or redacted projection; never raw private reasoning)
- `react.tool.started`, `react.tool.completed`
- `answer.delta`
- `message.final`
- `turn.output.completed`
- `turn.interrupted`
- `error`, `pong`

Every turn frame carries `session_id` and `turn_id` when applicable. A busy/idempotent rejection uses the existing `error` frame with a stable machine-readable detail while preserving the human-readable message.

## Delivery rules

Web projection is a subscriber to core events. It must not execute tools, mutate memory, or own canonical persistence. Disconnecting a client must not cancel a completed canonical turn unless the client explicitly sends `turn.stop`.
