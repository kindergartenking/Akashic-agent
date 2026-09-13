# Data Model: Akashic Agent Core

## Ownership

`sessions.db` is the source of truth. `model-registry.sqlite3` stores versioned model and credential metadata. `memory/akasha.db` stores only derived features, graph/index state, and recall diagnostics; it can be removed and rebuilt.

## Entities

### Session

| Field | Meaning | Rules |
|---|---|---|
| `session_id` | Stable conversation key | Unique, non-empty |
| `scope` | Owner/channel isolation scope | Required; prevents cross-user reads |
| `channel_refs` | Associated Web/QQ identities | May contain multiple channel mappings |
| `status` | Active/closed | Closed sessions reject new turns |
| `created_at`, `updated_at` | Lifecycle timestamps | Monotonic updates |

### Turn

| Field | Meaning | Rules |
|---|---|---|
| `turn_id` | Unique request identity | Idempotency key within session |
| `session_id` | Parent session | Foreign key |
| `client_message_id` | Upstream delivery identity | Unique when supplied |
| `state` | `accepted`, `running`, `completed`, `failed`, `cancelled` | Only valid forward transitions |
| `source_channel` | Web or QQ | Stored for audit, not Agent branching |
| `started_at`, `completed_at` | Timing | Required at corresponding transitions |
| `error_code` | Sanitized failure reason | No secrets |

### Message

Canonical user, assistant, tool, or system content attached to a turn. It includes `message_id`, `turn_id`, `role`, `content`, optional `media`, `source_metadata`, and a monotonically increasing session sequence. Duplicate deliveries must resolve to the existing message/turn.

### ChannelEnvelope

Normalized inbound/outbound boundary: `channel`, `chat_id`, `sender_id`, `session_id`, `external_message_id`, `text`, `media`, `is_group`, `mentioned_bot`, `reply_target`, and `received_at`. Adapter validation occurs before it reaches the bus.

### ToolInvocation

`call_id`, `turn_id`, `tool_name`, validated `arguments`, `policy_decision`, `started_at`, `completed_at`, `status`, `redacted_result_preview`, and `error_code`. Raw credentials and unrestricted output are never persisted.

### MemoryRecall

Derived record containing `turn_id`, `session_id`, source message IDs, entry lane, `seed_score`, `head_score`, `tail_score`, graph path, continuation state, selection stage, token cost, and a recall generation/ticket. It never replaces canonical messages.

### ModelConfiguration

Versioned provider/model/reasoning/embedding references and capability metadata. Secret material is held by a server-side credential store; API responses expose only references and masked metadata. Updates use optimistic revision checks.

### StructuredEvent

`event_id`, `event_type`, `session_id`, `turn_id`, `channel`, `sequence`, `timestamp`, `payload`, `redaction_version`. Events are append-oriented and support recovery/audit without exposing secrets.

## State transitions

```text
accepted -> running -> completed
                    ├-> failed
                    └-> cancelled
```

Only `running` owns the per-session execution lease. A retry with the same idempotency identity returns the existing state; a different request while `running` receives `session_busy`.

## Memory derivation

After `completed`, a background or inline commit derives embeddings, lexical features, graph edges, and recall diagnostics. Rebuilding reads only canonical sessions/messages and is deterministic for a fixed model/index configuration. A failed derivation leaves the turn valid and records `memory_degraded`.
