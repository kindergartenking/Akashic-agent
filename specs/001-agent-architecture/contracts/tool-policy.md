# Tool Policy Contract

All model-requested tools pass through one registry and policy evaluator, independent of Web or QQ origin.

## Decision outcomes

- `allow`: schema and scope checks pass; execute with timeout and bounded output.
- `deny`: request is rejected before side effects; return stable reason and emit an audit event.
- `unsupported`: capability is intentionally outside this milestone (including `spawn` and `spawn_manage`).
- `error`: allowed tool began but failed or timed out; preserve failure state and redact output.

## Mandatory controls

1. Tool name must be registered and explicitly allowlisted.
2. Arguments must satisfy the tool schema and workspace/session scope.
3. Shell, file, network, and message tools require timeout, output-size limits, and sensitive-data redaction.
4. Tool calls are correlated by `session_id`, `turn_id`, and `call_id`.
5. Repeated identical calls are bounded by the ReAct loop guard.
6. `spawn` and `spawn_manage` are never exposed in the effective model schema and remain denied if manually requested.

The tool result sent to the model may contain a bounded diagnostic; the persisted event and Web/QQ projection must use the redacted preview.
