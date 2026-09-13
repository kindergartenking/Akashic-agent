from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.session_store import SessionStore


def test_turn_messages_persist_and_are_idempotent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")

    first_id = store.record_user_message("web:one", "turn-one", "hello")
    repeated_id = store.record_user_message("web:one", "turn-one", "hello")
    assert repeated_id == first_id

    store.mark_turn_started("turn-one")
    assistant_id = store.complete_turn(
        "web:one",
        "turn-one",
        "world",
        duration_ms=42,
        tool_chain=[{
            "text": "",
            "calls": [{
                "call_id": "call-1",
                "name": "echo",
                "status": "success",
                "arguments": {"text": "hello"},
                "result": "echo:hello",
            }],
        }],
    )
    assert store.complete_turn(
        "web:one",
        "turn-one",
        "world",
        duration_ms=42,
    ) == assistant_id

    sessions = store.list_sessions()
    assert sessions["total"] == 1
    assert sessions["items"][0]["key"] == "web:one"
    assert sessions["items"][0]["message_count"] == 2
    assert sessions["items"][0]["first_message_content"] == "hello"
    assert sessions["items"][0]["user_id"] == "local"

    history = store.list_messages("web:one")
    assert history["total"] == 2
    assert history["has_more"] is False
    assert history["before_seq"] is None
    assert [(row["seq"], row["role"], row["content"]) for row in history["items"]] == [
        (1, "user", "hello"),
        (2, "assistant", "world"),
    ]
    assert {row["turn_id"] for row in history["items"]} == {"turn-one"}
    assert history["items"][1]["turn_duration_ms"] == 42
    assert history["items"][1]["tool_chain"][0]["calls"][0]["name"] == "echo"

    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        turn = connection.execute(
            "SELECT session_key, status, final_response FROM turns WHERE id = ?",
            ("turn-one",),
        ).fetchone()
    assert turn == ("web:one", "completed", "world")


def test_history_uses_stable_seq_cursor(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    for index in range(3):
        turn_id = f"turn-{index}"
        store.record_user_message("web:page", turn_id, f"question {index}")
        store.complete_turn("web:page", turn_id, f"answer {index}")

    latest = store.list_messages("web:page", page_size=2)
    assert [row["seq"] for row in latest["items"]] == [5, 6]
    assert latest["has_more"] is True
    assert latest["before_seq"] == 5

    older = store.list_messages(
        "web:page",
        page_size=2,
        before_seq=latest["before_seq"],
    )
    assert [row["seq"] for row in older["items"]] == [3, 4]
    assert older["has_more"] is True
    assert older["before_seq"] == 3


def test_turn_id_cannot_cross_sessions(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.record_user_message("web:first", "turn-shared", "one")
    try:
        store.record_user_message("web:second", "turn-shared", "two")
    except ValueError as error:
        assert "其他 session" in str(error)
    else:
        raise AssertionError("cross-session turn id was accepted")
