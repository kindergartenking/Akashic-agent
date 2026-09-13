# QQ Channel Adapter Contract

The QQ adapter normalizes OneBot 11-style events. The core runtime sees only `ChannelEnvelope` and never imports QQ SDK types.

## Inbound mapping

| QQ event | Normalized behavior |
|---|---|
| Private message | `channel=qq`, `is_group=false`, direct text/media processing |
| Group message with bot mention | `is_group=true`, `mentioned_bot=true`; strip bot mention and process |
| Group message without bot mention | Ignore before bus admission; create no session or turn |
| Duplicate external message ID | Return prior admission result; do not execute tools again |
| Unsupported/invalid payload | Emit sanitized `channel_rejected` event and no turn |

`session_id` is derived from a configured account/peer scope and chat identity. The mapping must be deterministic and must not allow one QQ user or group to read another scope.

## Outbound mapping

Core `answer.delta` may be buffered or chunked according to QQ message limits. `message.final`, errors, and interruption states map to one or more outbound messages with a correlation to `turn_id` and external reply target. Delivery failures are recorded as events and never rewrite the canonical assistant message.

## Adapter boundary

The adapter owns authentication, gateway reconnect, mention parsing, message-size limits, rate limits, and outbound retry policy. It does not decide Agent tools, memory selection, model choice, or child-agent permissions.
