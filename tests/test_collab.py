"""Collaboration identity, owner policy, isolation from agents, and a loopback round trip."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import http.client
import os
import shutil
import sqlite3
import subprocess
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import pytest

import llm_bus_collab_identity as identity
from llm_bus_collab_daemon import Daemon
from llm_bus_collab_responder import Reply, command, secret_findings
from llm_bus_collab_state import (
    CollabError,
    CollabState,
    Invite,
    NewMessage,
    Peer,
    decode_invite,
    effective_mode,
    encode_invite,
    exposure_allowed,
    grant,
)
from llm_bus_collab_web import serve_ui
from llm_bus_store import BusError, SenderContext, Store

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from llm_bus_collab_transport import Frame

needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen missing")
posix_only = pytest.mark.skipif(os.name == "nt", reason="fake responder is a POSIX script")


@pytest.fixture(autouse=True, name="isolated_collab")
def isolated_collab_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "collab"
    monkeypatch.setenv("LLM_BUS_COLLAB_HOME", str(home))
    monkeypatch.setenv("LLM_BUS_COLLAB_LOCAL_ONLY", "1")
    monkeypatch.setenv(identity.TEST_KEYS_ENV, str(tmp_path / "keys"))
    (tmp_path / "keys").mkdir()
    return home


def make_key(tmp_path: Path, user: str, *, publish: bool = True) -> Path:
    key = tmp_path / f"{user}_key"
    subprocess.run(
        [cast("str", shutil.which("ssh-keygen")), "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    if publish:
        published = tmp_path / "keys" / f"{user}.keys"
        existing = published.read_text() if published.exists() else ""
        published.write_text(existing + key.with_name(key.name + ".pub").read_text())
    return key


def sample_invite(
    *,
    host_user: str = "alice",
    mode: str = "readonly",
    expires_at: str = "2999-01-01T00:00:00.000Z",
) -> Invite:
    return Invite(
        v=0,
        ticket="ticket",
        nonce="n" * 32,
        host_user=host_user,
        joiner_user="bob",
        repository_name="repo",
        grant=mode,
        expires_at=expires_at,
    )


def sample_peer(
    *, mode: str = "full", full_until: str | None = None, session: str | None = None
) -> Peer:
    return Peer(
        id=1,
        pair="p",
        github_user="bob",
        repository="/repo",
        endpoint_id="e",
        ticket=None,
        mode=mode,
        full_until=full_until,
        review_replies=True,
        agent_replies=False,
        wake_on_answer=False,
        their_grant=None,
        session=session,
        confirm_code="c",
        created_at="now",
        revoked_at=None,
    )


def test_invite_round_trips_and_rejects_tampering() -> None:
    invite = sample_invite()
    assert decode_invite(encode_invite(invite)) == invite
    with pytest.raises(CollabError, match="expired"):
        decode_invite(encode_invite(sample_invite(expires_at="2000-01-01T00:00:00.000Z")))
    with pytest.raises(CollabError):
        decode_invite(encode_invite(sample_invite(mode="root")))
    with pytest.raises(CollabError):
        decode_invite(encode_invite(sample_invite(host_user="-bad")))
    with pytest.raises(CollabError):
        decode_invite(
            encode_invite(cast("Invite", cast("object", {**sample_invite(), "extra": "x"})))
        )
    with pytest.raises(CollabError):
        decode_invite("https://example.com/not-an-invite")


def test_full_mode_requires_expiry_and_falls_back_to_monitor() -> None:
    with pytest.raises(CollabError):
        grant("full")
    with pytest.raises(CollabError):
        grant("admin")
    choice = grant("full", 5)
    peer = sample_peer(full_until=choice.full_until)
    assert effective_mode(peer) == "full"
    assert effective_mode(sample_peer(full_until="2000-01-01T00:00:00.000Z")) == "monitor"
    assert effective_mode({**peer, "revoked_at": "now"}) is None


def test_invites_are_single_use_and_bound_to_one_user(isolated_collab: Path) -> None:
    state = CollabState(isolated_collab)
    nonce, _ = state.create_invite("bob", grant("readonly"), "/repo", "claude:me")
    assert state.invite_is_open(nonce, "bob")
    assert not state.invite_is_open(nonce, "mallory")
    with pytest.raises(CollabError):
        state.claim_invite(nonce, "mallory")
    choice, repository, session = state.claim_invite(nonce, "bob")
    assert (choice.mode, repository, session) == ("readonly", "/repo", "claude:me")
    with pytest.raises(CollabError):
        state.claim_invite(nonce, "bob")
    late, _ = state.create_invite("bob", grant("monitor"), "/repo", None)
    with contextlib.closing(sqlite3.connect(state.path)) as db:
        db.execute(
            "UPDATE invites SET expires_at = '2000-01-01T00:00:00.000Z' WHERE nonce = ?", (late,)
        )
        db.commit()
    with pytest.raises(CollabError, match="expired"):
        state.claim_invite(late, "bob")


@needs_ssh
def test_signature_binds_github_key_to_exact_transcript(tmp_path: Path) -> None:
    alice = make_key(tmp_path, "alice")
    make_key(tmp_path, "mallory")
    handshake = identity.Handshake("host", "joiner", "nonce", "bob", "alice")
    data = identity.transcript("joiner", handshake)
    signature = identity.sign(alice, data)
    assert identity.verify("alice", data, signature) is not None
    assert identity.verify("mallory", data, signature) is None
    assert identity.verify("nobody", data, signature) is None
    other = identity.transcript("joiner", handshake._replace(joiner_id="attacker"))
    assert identity.verify("alice", other, signature) is None
    assert identity.verify("alice", identity.transcript("host", handshake), signature) is None
    assert identity.confirm_code(handshake) == identity.confirm_code(handshake)


def test_secret_scan_flags_credentials_but_not_ordinary_text() -> None:
    assert "GitHub token" in secret_findings("token ghp_" + "a1B2" * 10, {})
    assert "private key" in secret_findings("-----BEGIN OPENSSH PRIVATE KEY-----", {})
    assert "local environment secret" in secret_findings(
        "value is s3cr3t-value-123", {"DEPLOY_TOKEN": "s3cr3t-value-123"}
    )
    assert secret_findings("I'm editing src/api.py at commit " + "a" * 40, {}) == []


def responder_peer(session: str | None) -> Peer:
    return sample_peer(mode="readonly", session=session)


def test_responder_tool_limits_are_cli_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BUS_COLLAB_CLAUDE", "claude")
    readonly = command(responder_peer("claude:abc"), "readonly", "hi")
    assert readonly[readonly.index("--resume") + 1] == "abc"
    for flag in ("--fork-session", "--restricted", "--strict-mcp-config"):
        assert flag in readonly
    assert readonly[readonly.index("--tools") + 1] == "Read,Grep,Glob"
    assert readonly[readonly.index("--permission-mode") + 1] == "dontAsk"
    assert "--resume" not in command(responder_peer(None), "isolated", "hi")
    readwrite = command(responder_peer("claude:abc"), "readwrite", "hi")
    assert readwrite[readwrite.index("--tools") + 1] == "Read,Grep,Glob,Edit,Write"
    with pytest.raises(CollabError):
        command(responder_peer("codex:abc"), "readonly", "hi")


def add_full_peer(
    state: CollabState, session: str, *, mode: str = "full", agent_replies: bool = False
) -> None:
    with contextlib.closing(sqlite3.connect(state.path)) as db:
        db.execute(
            "INSERT INTO peers(pair, github_user, repository, endpoint_id, key_fingerprint, mode, "
            "full_until, review_replies, agent_replies, session, confirm_code, created_at) "
            "VALUES ('p', 'bob', '/repo', 'e', 'f', ?, ?, 1, ?, ?, 'c', 'now')",
            (mode, grant("full", 5).full_until, int(agent_replies), session),
        )
        db.commit()


def test_answers_reach_the_asking_agent_only_while_allowed(
    tmp_path: Path, isolated_collab: Path
) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    me = store.register("claude", "me", str(tmp_path))
    state = CollabState(isolated_collab)
    add_full_peer(state, me, mode="readonly", agent_replies=True)
    store.deliver_collab("collab:reply:bob", me, "answer")
    store.deliver_collab("collab:bob", me, "unsolicited")
    assert [m["sender"] for m in store.inbox(me)] == ["collab:reply:bob"]
    assert not exposure_allowed("collab:reply:bob", "claude:someone-else")
    state.set_mode(state.peers()[0]["id"], grant("readonly"))
    assert store.inbox(me) == []
    state.set_mode(state.peers()[0]["id"], grant("readonly", agent_replies=True))
    state.revoke(state.peers()[0]["id"])
    assert store.inbox(me) == []


def test_only_the_first_answer_to_an_own_question_is_delivered(
    tmp_path: Path, isolated_collab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_wake(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "skipped", "via": "test"}

    monkeypatch.setattr("llm_bus_collab_daemon.wake", no_wake)
    store = Store(tmp_path / "bus.sqlite3")
    me = store.register("claude", "me", str(tmp_path))
    state = CollabState(isolated_collab)
    add_full_peer(state, me, mode="monitor", agent_replies=True)
    peer = state.peers()[0]
    daemon = Daemon(state, store)
    _ = state.add_message(peer["id"], NewMessage("out", "request", "q1", "How?", "sent"))

    def arrive(uid: str, in_reply_to: str | None) -> str:
        frame: Frame = {
            "type": "msg",
            "pair": "p",
            "uid": uid,
            "kind": "reply" if in_reply_to else "request",
            "in_reply_to": in_reply_to,
            "body": f"text {uid}",
        }
        assert asyncio.run(daemon.handle("e", frame)) == {"ok": True}
        return next(m["state"] for m in state.messages() if m["uid"] == uid)

    assert arrive("a1", "q1") == "delivered"
    assert arrive("a2", "q1") == "unread"
    assert arrive("a3", "never-asked") == "unread"
    assert arrive("a4", None) == "unread"
    answers = [m for m in store.inbox(me) if m["sender"] == "collab:reply:bob"]
    assert len(answers) == 1
    assert "untrusted peer data" in answers[0]["body"]
    assert "text a1" in answers[0]["body"]
    state.set_mode(peer["id"], grant("monitor"))
    _ = state.add_message(peer["id"], NewMessage("out", "request", "q2", "And?", "sent"))
    assert arrive("a5", "q2") == "unread"
    assert all(m["sender"] != "collab:reply:bob" for m in store.inbox(me))


def test_automatic_answers_wake_the_owner_agent_only_when_held_or_chosen(
    tmp_path: Path, isolated_collab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_wake(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "skipped", "via": "test"}

    replies = iter(["We use iroh.", "token ghp_" + "a" * 36, "Still iroh."])

    def fake_respond(*_args: object) -> Reply:
        return Reply(next(replies), None)

    monkeypatch.setattr("llm_bus_collab_daemon.wake", no_wake)
    monkeypatch.setattr("llm_bus_collab_daemon.respond", fake_respond)
    store = Store(tmp_path / "bus.sqlite3")
    me = store.register("claude", "me", str(tmp_path))
    state = CollabState(isolated_collab)
    add_full_peer(state, me, mode="readonly")
    state.set_mode(state.peers()[0]["id"], grant("readonly", review_replies=False))
    daemon = Daemon(state, store)

    async def ask(uid: str) -> None:
        frame: Frame = {"type": "msg", "pair": "p", "uid": uid, "kind": "request", "body": "?"}
        assert await daemon.handle("e", frame) == {"ok": True}
        await wait_until(
            lambda: next(m for m in state.messages() if m["uid"] == uid)["state"] != "responding"
        )

    asyncio.run(ask("plain"))
    assert store.inbox(me) == []
    asyncio.run(ask("secret"))
    notices = store.inbox(me)
    assert len(notices) == 1
    assert "review" in notices[0]["body"]
    state.set_mode(
        state.peers()[0]["id"], grant("readonly", review_replies=False, wake_on_answer=True)
    )
    asyncio.run(ask("woken"))
    assert len(store.inbox(me)) == 2


def test_existing_state_gains_agent_replies_column(tmp_path: Path) -> None:
    home = tmp_path / "old"
    home.mkdir()
    with contextlib.closing(sqlite3.connect(home / "collab.sqlite3")) as db:
        db.executescript(
            "CREATE TABLE peers (id INTEGER PRIMARY KEY, pair TEXT NOT NULL UNIQUE, "
            "github_user TEXT NOT NULL, repository TEXT NOT NULL, endpoint_id TEXT NOT NULL, "
            "ticket TEXT, key_fingerprint TEXT NOT NULL, mode TEXT NOT NULL, full_until TEXT, "
            "review_replies INTEGER NOT NULL, their_grant TEXT, session TEXT, "
            "confirm_code TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);"
            "INSERT INTO peers(pair, github_user, repository, endpoint_id, key_fingerprint, mode, "
            "review_replies, confirm_code, created_at) "
            "VALUES ('p', 'bob', '/repo', 'e', 'f', 'readonly', 1, 'c', 'now');"
        )
    peer = CollabState(home).peers()[0]
    assert (peer["agent_replies"], peer["wake_on_answer"]) == (False, False)


def test_peer_text_reaches_agents_only_in_full_mode(tmp_path: Path, isolated_collab: Path) -> None:
    store = Store(tmp_path / "bus.sqlite3")
    me = store.register("claude", "me", str(tmp_path))
    state = CollabState(isolated_collab)
    add_full_peer(state, me)
    store.deliver_collab("collab:_daemon", me, "notice without peer text")
    store.deliver_collab("collab:bob", me, "peer text")
    assert [m["sender"] for m in store.inbox(me)] == ["collab:_daemon", "collab:bob"]
    state.set_mode(state.peers()[0]["id"], grant("readonly"))
    assert [m["sender"] for m in store.inbox(me)] == ["collab:_daemon"]
    assert store.pending_count(me) == 1
    assert not exposure_allowed("collab:bob", "claude:someone-else")
    assert all(a["address"] != "collab:bob" for a in store.agents())
    with pytest.raises(BusError):
        store.send(SenderContext("claude", "me", str(tmp_path)), "collab:bob", "hello")


def fetch(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> int:
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=5)
    try:
        connection.request("POST" if data is not None else "GET", parts.path, data, headers or {})
        return connection.getresponse().status
    finally:
        connection.close()


def test_owner_page_requires_token_host_and_origin(tmp_path: Path, isolated_collab: Path) -> None:
    state = CollabState(isolated_collab)
    server, port = serve_ui(state, Store(tmp_path / "bus.sqlite3"), "t" * 24, lambda: "ticket")
    try:
        base = f"http://127.0.0.1:{port}"
        assert fetch(f"{base}/{'t' * 24}/") == 200
        assert fetch(f"{base}/wrong-token-wrong-token/") == 404
        assert fetch(f"{base}/{'t' * 24}/", headers={"Host": "evil.example"}) == 404
        form = b"user=bob&mode=monitor"
        assert (
            fetch(
                f"{base}/{'t' * 24}/invite", data=form, headers={"Origin": "https://evil.example"}
            )
            == 404
        )
        add_full_peer(state, "claude:me", mode="monitor")
        unconfirmed = b"mode=full&full_minutes=5"
        assert fetch(f"{base}/{'t' * 24}/peer/1/mode", data=unconfirmed) == 400
        same_origin = {"Origin": base}
        assert fetch(f"{base}/{'t' * 24}/peer/1/mode", data=unconfirmed, headers=same_origin) == 400
        assert effective_mode(state.peers()[0]) == "monitor"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", f"/{'t' * 24}/")
        response = connection.getresponse()
        assert response.getheader("Referrer-Policy") == "same-origin"
        script = response.read().decode().split("<script>")[1].split("</script>")[0]
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"script-src 'sha256-{digest}'" in (
            response.getheader("Content-Security-Policy") or ""
        )
        connection.close()
    finally:
        server.shutdown()


def committed_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "api.py").write_text("VALUE = 1\n")
    git = cast("str", shutil.which("git"))
    for args in (["init", "-q"], ["add", "api.py"], ["commit", "-q", "-m", "init"]):
        subprocess.run(
            [git, "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repository), *args],
            check=True,
        )
    return repository


def invite_text(state: CollabState, ticket: str, repository: Path) -> str:
    nonce, expires = state.create_invite(
        "bob", grant("readonly", review_replies=False), str(repository), "claude:a"
    )
    return encode_invite(
        Invite(
            v=0,
            ticket=ticket,
            nonce=nonce,
            host_user="alice",
            joiner_user="bob",
            repository_name="repo",
            grant="readonly",
            expires_at=expires,
        )
    )


async def wait_until(check: Callable[[], bool]) -> None:
    for _ in range(100):
        if check():
            return
        await asyncio.sleep(0.1)
    pytest.fail("condition not reached")


async def pair_and_exchange(states: dict[str, CollabState], text: str, repository: Path) -> None:
    alice, bob, mallory = states["alice"], states["bob"], states["mallory"]
    impostor = mallory.add_join(text, str(repository), None)
    mallory.approve_join(impostor, grant("monitor"))
    await wait_until(lambda: mallory.join(impostor)[3] == "failed")
    assert alice.peers() == []
    join = bob.add_join(text, str(repository), "claude:b")
    bob.approve_join(join, grant("monitor"))
    await wait_until(lambda: bob.join(join)[3] == "connected")
    assert alice.peers()[0]["confirm_code"] == bob.join(join)[4]
    peer_id = bob.peers()[0]["id"]
    bob.add_message(peer_id, NewMessage("out", "request", "u1", "What are you on?", "queued"))
    await wait_until(lambda: any(m["state"] == "unread" for m in bob.messages()))
    reply = next(m for m in bob.messages() if m["direction"] == "in")
    assert (reply["kind"], reply["body"]) == ("reply", "I am editing src/api.py")
    alice.revoke(alice.peers()[0]["id"])
    bob.add_message(peer_id, NewMessage("out", "request", "u2", "Still there?", "queued"))
    await wait_until(lambda: any(m["state"] == "rejected" for m in bob.messages()))


@needs_ssh
@posix_only
def test_two_daemons_pair_reject_impersonation_and_respond(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = committed_repository(tmp_path)
    fake = tmp_path / "fakeclaude"
    fake.write_text('#!/bin/sh\necho \'{"result":"I am editing src/api.py","is_error":false}\'\n')
    fake.chmod(0o700)
    monkeypatch.setenv("LLM_BUS_COLLAB_CLAUDE", str(fake))
    states: dict[str, CollabState] = {}
    daemons: dict[str, Daemon] = {}
    for user in ("alice", "bob", "mallory"):
        states[user] = CollabState(tmp_path / user)
        states[user].set_identity(user, make_key(tmp_path, user))
        daemons[user] = Daemon(states[user], Store(tmp_path / f"{user}.sqlite3"))
    states["mallory"].set_identity("bob", tmp_path / "mallory_key")

    async def scenario() -> None:
        tasks = [asyncio.create_task(d.run()) for d in daemons.values()]
        try:
            await wait_until(lambda: all(d.transport is not None for d in daemons.values()))
            transport = daemons["alice"].transport
            assert transport is not None
            await pair_and_exchange(
                states, invite_text(states["alice"], transport.ticket(), repository), repository
            )
        finally:
            for task in tasks:
                _ = task.cancel()
            _ = await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
