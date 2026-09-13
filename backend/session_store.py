"""Durable session, turn and message storage for the chat MVP.

The original project keeps this data in ``sessions.db``.  This implementation
uses the same public column names consumed by the web UI, while adding the
``turn_id`` and ``user_id`` fields needed to correlate one request with its
persisted user/assistant messages.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_consolidated INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    user_id TEXT NOT NULL DEFAULT 'local'
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    final_response TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_turns_session_created
                    ON turns(session_key, created_at, id);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL DEFAULT '',
                    tool_chain TEXT,
                    extra TEXT,
                    ts TEXT NOT NULL,
                    UNIQUE(session_key, seq),
                    UNIQUE(session_key, turn_id, role)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_seq
                    ON messages(session_key, seq);
                """
            )
            # Additive compatibility for a database created by an earlier MVP
            # build.  SQLite has no IF NOT EXISTS form for ADD COLUMN.
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "user_id" not in columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local'"
                )
            message_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(messages)")
            }
            if "turn_id" not in message_columns:
                # A legacy empty MVP database can be upgraded in place.  If it
                # already contains rows, use a stable synthetic turn per row.
                connection.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT")
                rows = connection.execute(
                    "SELECT id, session_key FROM messages WHERE turn_id IS NULL"
                ).fetchall()
                for row in rows:
                    turn_id = f"legacy-{row['id']}"
                    connection.execute(
                        "INSERT OR IGNORE INTO turns(id, session_key, status, input_json, created_at) "
                        "VALUES (?, ?, 'completed', '{}', CURRENT_TIMESTAMP)",
                        (turn_id, row["session_key"]),
                    )
                    connection.execute(
                        "UPDATE messages SET turn_id = ? WHERE id = ?",
                        (turn_id, row["id"]),
                    )
            connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def record_user_message(
        self,
        session_id: str,
        turn_id: str,
        content: str,
        *,
        user_id: str = "local",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Atomically admit a session, queued turn and user message.

        Re-sending the same turn is idempotent, which prevents a reconnect or
        browser retry from creating duplicate user rows.
        """

        session_id = session_id.strip()
        turn_id = turn_id.strip()
        if not session_id or not turn_id:
            raise ValueError("session_id 和 turn_id 不能为空")
        now = self._now()
        message_id = f"msg-{uuid4().hex}"
        metadata_payload = dict(metadata or {})
        metadata_payload.setdefault("channel", "web")
        metadata_payload.setdefault("user_id", user_id or "local")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO sessions(key, created_at, updated_at, metadata, user_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    metadata = excluded.metadata,
                    user_id = excluded.user_id
                """,
                (
                    session_id,
                    now,
                    now,
                    json.dumps(metadata_payload, ensure_ascii=False, separators=(",", ":")),
                    user_id or "local",
                ),
            )
            existing = connection.execute(
                "SELECT id FROM messages WHERE session_key = ? AND turn_id = ? AND role = 'user'",
                (session_id, turn_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return str(existing["id"])
            connection.execute(
                """
                INSERT INTO turns(id, session_key, status, input_json, created_at)
                VALUES (?, ?, 'queued', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    turn_id,
                    session_id,
                    json.dumps({"text": content}, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            turn_owner = connection.execute(
                "SELECT session_key FROM turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if turn_owner is None or str(turn_owner["session_key"]) != session_id:
                connection.rollback()
                raise ValueError("turn_id 已属于其他 session")
            seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_key = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO messages(id, session_key, seq, turn_id, role, content, ts)
                VALUES (?, ?, ?, ?, 'user', ?, ?)
                """,
                (message_id, session_id, seq, turn_id, content, now),
            )
            connection.commit()
        return message_id

    def mark_turn_started(self, turn_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE turns SET status = 'running', started_at = ? WHERE id = ?",
                (self._now(), turn_id),
            )
            connection.commit()

    def complete_turn(
        self,
        session_id: str,
        turn_id: str,
        content: str,
        *,
        duration_ms: int | None = None,
        tool_chain: list[dict[str, Any]] | None = None,
    ) -> str:
        now = self._now()
        message_id = f"msg-{uuid4().hex}"
        extra = {"turn_id": turn_id}
        if duration_ms is not None:
            extra["turn_duration_ms"] = duration_ms
        tool_chain_json = (
            json.dumps(tool_chain, ensure_ascii=False, separators=(",", ":"))
            if tool_chain
            else None
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE turns SET status = 'completed', final_response = ?,
                    completed_at = ? WHERE id = ? AND session_key = ?
                """,
                (content, now, turn_id, session_id),
            )
            existing = connection.execute(
                "SELECT id FROM messages WHERE session_key = ? AND turn_id = ? AND role = 'assistant'",
                (session_id, turn_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE messages SET content = ?, tool_chain = COALESCE(?, tool_chain), extra = ?, ts = ? WHERE id = ?",
                    (
                        content,
                        tool_chain_json,
                        json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
                        now,
                        existing["id"],
                    ),
                )
                message_id = str(existing["id"])
            else:
                seq = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_key = ?",
                        (session_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    INSERT INTO messages(id, session_key, seq, turn_id, role, content, tool_chain, extra, ts)
                    VALUES (?, ?, ?, ?, 'assistant', ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        session_id,
                        seq,
                        turn_id,
                        content,
                        tool_chain_json,
                        json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
                        now,
                    ),
                )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE key = ?",
                (now, session_id),
            )
            connection.commit()
        return message_id

    def fail_turn(self, turn_id: str, error: Exception, *, status: str = "failed") -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE turns SET status = ?, error_json = ?, completed_at = ? WHERE id = ?",
                (
                    status,
                    json.dumps({"message": str(error)}, ensure_ascii=False),
                    self._now(),
                    turn_id,
                ),
            )
            connection.commit()

    def list_sessions(self, *, page: int = 1, page_size: int = 80) -> dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        with closing(self._connect()) as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            rows = connection.execute(
                """
                SELECT s.key, s.created_at, s.updated_at, s.user_id,
                       COUNT(m.id) AS message_count,
                       (SELECT content FROM messages first_m
                        WHERE first_m.session_key = s.key AND first_m.role = 'user'
                        ORDER BY first_m.seq LIMIT 1) AS first_message_content
                FROM sessions s LEFT JOIN messages m ON m.session_key = s.key
                GROUP BY s.key ORDER BY s.updated_at DESC, s.key DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, (page - 1) * page_size),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
        }

    def list_messages(
        self,
        session_id: str,
        *,
        page_size: int = 50,
        before_seq: int | None = None,
    ) -> dict[str, Any]:
        page_size = max(1, min(page_size, 200))
        with closing(self._connect()) as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_key = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            if before_seq is None:
                rows = connection.execute(
                    "SELECT * FROM messages WHERE session_key = ? ORDER BY seq DESC LIMIT ?",
                    (session_id, page_size),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = connection.execute(
                    "SELECT * FROM messages WHERE session_key = ? AND seq < ? ORDER BY seq DESC LIMIT ?",
                    (session_id, before_seq, page_size),
                ).fetchall()
                rows = list(reversed(rows))
        items = [self._message_dict(row) for row in rows]
        has_more = bool(items and int(items[0]["seq"]) > 1)
        return {
            "items": items,
            "total": total,
            "has_more": has_more,
            "before_seq": int(items[0]["seq"]) if has_more else None,
        }

    def all_context_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return the complete ordered conversation for context assembly."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE session_key = ? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
        return [self._message_dict(row) for row in rows]

    def completed_turns(self) -> list[dict[str, Any]]:
        """Return canonical user/assistant pairs in one stable causal order.

        Akasha deliberately does not treat its own sidecar database as the
        source of truth.  This method is the small adapter from this MVP's
        session schema to that rule: a row becomes a memory turn only after
        both messages and the ``completed`` turn status are durable.
        """

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    t.id AS turn_id,
                    t.session_key,
                    t.created_at,
                    t.started_at,
                    t.completed_at,
                    u.id AS user_message_id,
                    u.seq AS user_seq,
                    u.content AS user_text,
                    u.ts AS user_timestamp,
                    a.id AS assistant_message_id,
                    a.seq AS assistant_seq,
                    a.content AS assistant_text,
                    a.ts AS assistant_timestamp
                FROM turns AS t
                JOIN messages AS u
                    ON u.session_key = t.session_key
                   AND u.turn_id = t.id
                   AND u.role = 'user'
                JOIN messages AS a
                    ON a.session_key = t.session_key
                   AND a.turn_id = t.id
                   AND a.role = 'assistant'
                WHERE t.status = 'completed'
                ORDER BY t.completed_at ASC, t.session_key ASC, u.seq ASC, t.id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def completed_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        user_message_id: str | None = None,
        assistant_message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve exactly one durable source turn for an Akasha commit.

        The optional message IDs make the commit boundary explicit and catch
        accidental cross-turn wiring before any derived memory state changes.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    t.id AS turn_id,
                    t.session_key,
                    t.created_at,
                    t.started_at,
                    t.completed_at,
                    u.id AS user_message_id,
                    u.seq AS user_seq,
                    u.content AS user_text,
                    u.ts AS user_timestamp,
                    a.id AS assistant_message_id,
                    a.seq AS assistant_seq,
                    a.content AS assistant_text,
                    a.ts AS assistant_timestamp
                FROM turns AS t
                JOIN messages AS u
                    ON u.session_key = t.session_key
                   AND u.turn_id = t.id
                   AND u.role = 'user'
                JOIN messages AS a
                    ON a.session_key = t.session_key
                   AND a.turn_id = t.id
                   AND a.role = 'assistant'
                WHERE t.status = 'completed' AND t.session_key = ? AND t.id = ?
                """,
                (session_id, turn_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if user_message_id and str(result["user_message_id"]) != user_message_id:
            raise ValueError("Akasha commit 的 user_message_id 与事实源不一致")
        if assistant_message_id and str(result["assistant_message_id"]) != assistant_message_id:
            raise ValueError("Akasha commit 的 assistant_message_id 与事实源不一致")
        return result

    def fetch_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Fetch messages by persisted id or the ``session:seq`` source ref."""
        values = [str(item).strip() for item in ids if str(item).strip()]
        if not values:
            return []
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        with closing(self._connect()) as connection:
            for value in values:
                row = connection.execute("SELECT * FROM messages WHERE id = ?", (value,)).fetchone()
                if row is None and ":" in value:
                    session_key, seq_text = value.rsplit(":", 1)
                    try:
                        row = connection.execute(
                            "SELECT * FROM messages WHERE session_key = ? AND seq = ?",
                            (session_key, int(seq_text)),
                        ).fetchone()
                    except ValueError:
                        row = None
                if row is not None and str(row["id"]) not in seen:
                    seen.add(str(row["id"]))
                    result.append(self._message_dict(row))
        return result

    def fetch_by_ids_with_context(self, ids: list[str], context: int) -> list[dict[str, Any]]:
        hits = self.fetch_by_ids(ids)
        if not hits or context <= 0:
            for item in hits:
                item["in_source_ref"] = True
            return hits
        wanted = {(str(item["session_key"]), int(item["seq"])) for item in hits}
        result: list[dict[str, Any]] = []
        with closing(self._connect()) as connection:
            for session_key, seq in sorted(wanted):
                rows = connection.execute(
                    "SELECT * FROM messages WHERE session_key = ? AND seq BETWEEN ? AND ? ORDER BY seq",
                    (session_key, max(1, seq - int(context)), seq + int(context)),
                ).fetchall()
                for row in rows:
                    item = self._message_dict(row)
                    item["in_source_ref"] = (str(row["session_key"]), int(row["seq"])) in wanted
                    result.append(item)
        return result

    def search_messages(
        self,
        query: str,
        *,
        session_key: str | None = None,
        role: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        terms = [term for term in str(query).split() if term] or [str(query)]
        limit = max(1, min(int(limit), 50))
        offset = max(0, int(offset))
        where = ["content LIKE ?"]
        params: list[Any] = [f"%{terms[0]}%"]
        # OR semantics match the original grep-like tool.
        for term in terms[1:]:
            where.append("content LIKE ?")
            params.append(f"%{term}%")
        clause = "(" + " OR ".join(where) + ")"
        filters: list[str] = []
        filter_params: list[Any] = []
        if session_key:
            filters.append("session_key = ?")
            filter_params.append(session_key)
        if role:
            filters.append("role = ?")
            filter_params.append(role)
        sql_where = " AND ".join([clause, *filters])
        with closing(self._connect()) as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM messages WHERE {sql_where}",
                tuple(params + filter_params),
            ).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM messages WHERE {sql_where} ORDER BY seq DESC LIMIT ? OFFSET ?",
                tuple(params + filter_params + [limit, offset]),
            ).fetchall()
        return [self._message_dict(row) for row in rows], total

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if row["extra"]:
            try:
                parsed = json.loads(str(row["extra"]))
                if isinstance(parsed, dict):
                    extra = parsed
            except json.JSONDecodeError:
                pass
        tool_chain: list[Any] = []
        if row["tool_chain"]:
            try:
                parsed_chain = json.loads(str(row["tool_chain"]))
                if isinstance(parsed_chain, list):
                    tool_chain = parsed_chain
            except json.JSONDecodeError:
                pass
        return {
            "id": str(row["id"]),
            "session_key": str(row["session_key"]),
            "seq": int(row["seq"]),
            "turn_id": str(row["turn_id"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
            "timestamp": str(row["ts"]),
            "turn_duration_ms": extra.get("turn_duration_ms"),
            "tool_chain": tool_chain,
            "extra": extra,
        }


__all__ = ["SessionStore"]
