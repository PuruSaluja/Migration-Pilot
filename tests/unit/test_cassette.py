"""P1-11: cassettes (decision B6): a deterministic JSONL format and a replay runtime that
fails loudly when the request, or what the host does, no longer matches the recording."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from runtime_helpers import ANSWER, OPUS, SONNET, ctx, guard, host, req

from mpilot.agents.cassette import (
    CASSETTE_VERSION,
    CassetteEntry,
    CassetteRuntime,
    read_cassette,
    write_cassette,
)
from mpilot.agents.fake_runtime import (
    FakeRuntime,
    RaiseKind,
    ScriptedCall,
    ScriptedToolCall,
    ScriptedTurn,
    tool,
)
from mpilot.agents.runtime import (
    AgentRuntime,
    ModelChoice,
    RoleCallRequest,
    RoleCallResult,
    StopReason,
    check_result,
)
from mpilot.core.errors import CassetteMismatchError, ScriptDivergedError, UsageLimitError
from mpilot.core.types import Effort, LLMCallId, Role, RunId, StepId, Tier, Usage
from mpilot.models import domain as d
from mpilot.testing.doubles import InMemoryToolHost


def run[T](coro: Any) -> T:
    result: T = asyncio.run(coro)
    return result


def keep(line: str) -> str:
    return line


SCRIPT = ScriptedCall(
    turns=(
        ScriptedTurn(tool_calls=(tool("view_file", path="a.py"),), usage=Usage(10, 5, 100, 20)),
        ScriptedTurn(
            tool_calls=(ScriptedToolCall(name="Bash", args={"command": "ls"}),),
            usage=Usage(4, 2),
        ),
        ScriptedTurn(final=ANSWER, usage=Usage(3, 1)),
    ),
    latency_ms=1234,
)


def recorded(script: ScriptedCall = SCRIPT, **req_kw: Any) -> tuple[CassetteEntry, RoleCallRequest]:
    """Record `script` the way a real recording would: play it live, keep what the host said."""
    request = req(**req_kw)
    live = FakeRuntime([script])
    h = host()
    with contextlib.suppress(UsageLimitError):  # a recorded limit is part of the recording
        run(live.call(request, h, guard()))
    entries = []
    for scripted, inv in zip(
        (c for t in script.turns for c in t.tool_calls), live.calls[0].invocations, strict=True
    ):
        entries.append(
            scripted.model_copy(
                update={
                    "expect_decision": inv.decision,
                    "expect_deny_code": inv.deny_code,
                    "expect_output": inv.output,
                }
            )
        )
    it = iter(entries)
    turns = tuple(
        t.model_copy(update={"tool_calls": tuple(next(it) for _ in t.tool_calls)})
        for t in script.turns
    )
    return CassetteEntry.for_request(request, script.model_copy(update={"turns": turns})), request


# ---------------------------------------------------------------- the file format


def test_a_cassette_round_trips_through_a_file(tmp_path: Path) -> None:
    entry, _ = recorded()
    other, _ = recorded(ScriptedCall.answer(ANSWER), user_prompt="another")
    path = tmp_path / "c" / "run.jsonl"
    write_cassette(path, [entry, other], redact=keep)
    assert read_cassette(path) == [entry, other]


def test_the_file_is_deterministic_jsonl_with_a_header(tmp_path: Path) -> None:
    entry, _ = recorded()
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    write_cassette(a, [entry], redact=keep)
    write_cassette(b, [entry], redact=keep)
    assert a.read_bytes() == b.read_bytes()
    first, second = a.read_text().splitlines()
    assert json.loads(first) == {"mpilot_cassette": CASSETTE_VERSION}
    assert json.loads(second)["request_hash"] == entry.request_hash
    assert a.read_text().endswith("\n")


def test_prompts_are_never_stored_only_their_hashes(tmp_path: Path) -> None:
    entry, _ = recorded(user_prompt="the secret repo text")
    path = tmp_path / "c.jsonl"
    write_cassette(path, [entry], redact=keep)
    text = path.read_text()
    assert "secret repo text" not in text
    assert "rules" not in json.loads(text.splitlines()[1])["parts"]


def test_the_redactor_runs_on_every_line_and_must_keep_it_valid(tmp_path: Path) -> None:
    entry, _ = recorded()
    path = tmp_path / "c.jsonl"
    write_cassette(path, [entry], redact=lambda line: line.replace("contents of", "REDACTED"))
    text = path.read_text()
    assert "REDACTED" in text  # the recorded tool output carried "contents of a.py"
    assert "contents of" not in text
    leaky = ScriptedCall.answer({"answer": "token ghp_abc123", "confidence": 1})
    write_cassette(
        path,
        [CassetteEntry.for_request(req(), leaky)],
        redact=lambda line: line.replace("ghp_abc123", "[REDACTED]"),
    )
    assert "ghp_" not in path.read_text()
    assert "[REDACTED]" in path.read_text()

    path.write_text("keep me")
    with pytest.raises(ValueError, match=r"Invalid JSON"):
        write_cassette(path, [entry], redact=lambda line: line[:-5])  # breaks the record
    assert path.read_text() == "keep me"  # nothing was written


def test_malformed_files_are_reported_with_their_line(tmp_path: Path) -> None:
    entry, _ = recorded()
    good = tmp_path / "good.jsonl"
    write_cassette(good, [entry], redact=keep)
    lines = good.read_text().splitlines()

    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(ValueError, match="empty cassette"):
        read_cassette(empty)

    wrong = tmp_path / "wrong.jsonl"
    wrong.write_text('{"mpilot_cassette": 99}\n')
    with pytest.raises(ValueError, match=r"wrong.jsonl:1: not a version-1"):
        read_cassette(wrong)

    notjson = tmp_path / "notjson.jsonl"
    notjson.write_text("hello\n")
    with pytest.raises(ValueError, match=r"notjson.jsonl:1: header is not JSON"):
        read_cassette(notjson)

    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join([lines[0], lines[1], '{"request_hash": "nope"}']) + "\n")
    with pytest.raises(ValueError, match=r"bad.jsonl:3: bad cassette entry"):
        read_cassette(bad)


# ---------------------------------------------------------------- replay


def test_it_is_an_agent_runtime() -> None:
    runtime = CassetteRuntime([])
    assert isinstance(runtime, AgentRuntime)
    assert runtime.name == "cassette"
    runtime.check_lockdown_static()
    assert run(runtime.selftest()).passed  # type: ignore[attr-defined]


def test_a_recorded_call_replays_with_identical_results() -> None:
    entry, request = recorded()
    live_host = host()
    live: RoleCallResult = run(FakeRuntime([SCRIPT]).call(request, live_host, guard()))

    replay_host = host()
    replayed: RoleCallResult = run(CassetteRuntime([entry]).call(request, replay_host, guard()))

    check_result(request, replayed)
    assert replayed == live  # same output, usage, turns, model facts, even tool-call IDs here
    assert replayed.stop is StopReason.COMPLETED
    assert replayed.usage == Usage(17, 8, 100, 20)
    assert replayed.latency_ms == 1234
    assert [(c.tool, c.policy_decision) for c in replay_host.calls] == [
        ("view_file", d.PolicyDecision.ALLOW),
        ("Bash", d.PolicyDecision.DENY),
    ]


def test_a_replay_from_a_file_matches_a_replay_from_memory(tmp_path: Path) -> None:
    entry, request = recorded()
    path = tmp_path / "c.jsonl"
    write_cassette(path, [entry], redact=keep)
    from_file: RoleCallResult = run(CassetteRuntime.from_file(path).call(request, host(), guard()))
    from_memory: RoleCallResult = run(CassetteRuntime([entry]).call(request, host(), guard()))
    assert from_file == from_memory


def test_replay_matches_by_content_not_by_the_random_ids_of_a_new_run() -> None:
    entry, _ = recorded()
    fresh = req(
        context=ctx(
            run_id=RunId("run-completely-different"),
            role_call_id=LLMCallId("llm-fresh"),
            step_id=StepId("step-new-id"),
        )
    )
    result: RoleCallResult = run(CassetteRuntime([entry]).call(fresh, host(), guard()))
    assert result.call_id == "llm-fresh"  # the new call's own id, not the recorded one
    assert result.stop is StopReason.COMPLETED


def test_the_recorded_tool_calls_run_against_the_live_host() -> None:
    entry, request = recorded()
    h = host()
    run(CassetteRuntime([entry]).call(request, h, guard()))
    assert [c.input for c in h.calls] == [{"path": "a.py"}, {"command": "ls"}]  # rows, for real


def test_a_recorded_usage_limit_is_replayed() -> None:
    limited = ScriptedCall(
        turns=(ScriptedTurn(tool_calls=(tool("view_file", path="a"),)),),
        raises=RaiseKind.USAGE_LIMIT,
        raise_message="limit",
    )
    entry, request = recorded(limited)
    runtime, h = CassetteRuntime([entry]), host()
    with pytest.raises(UsageLimitError, match="limit"):
        run(runtime.call(request, h, guard()))
    assert len(h.calls) == 1
    assert isinstance(runtime.calls[0].error, UsageLimitError)


def test_replay_honours_the_budget_guard_like_a_live_call() -> None:
    entry, request = recorded()
    result: RoleCallResult = run(CassetteRuntime([entry]).call(request, host(), guard(max_turns=1)))
    assert result.stop is StopReason.BUDGET_STOP


# ---------------------------------------------------------------- loud mismatches


@pytest.mark.parametrize(
    ("change", "parts"),
    [
        ({"user_prompt": "the repo changed"}, "user_prompt"),
        ({"system_prompt": "new rules"}, "system_prompt"),
        ({"max_turns": 9}, "max_turns"),
        ({"tool_allowlist": frozenset({"view_file", "list_dir"})}, "tool_allowlist"),
        (
            {"choice": ModelChoice(tier=Tier.SONNET, effort=Effort.HIGH, model_id=SONNET)},
            "choice",
        ),
        ({"context": ctx(max_tool_calls=3)}, "max_tool_calls"),
        ({"context": ctx(step_target_files=frozenset({"a.py"}))}, "step_target_files"),
    ],
)
def test_a_changed_request_fails_loudly_and_says_what_changed(
    change: dict[str, Any], parts: str
) -> None:
    entry, _ = recorded()
    with pytest.raises(CassetteMismatchError, match=f"differs in: {parts}") as info:
        run(CassetteRuntime([entry]).call(req(**change), host(), guard()))
    assert "Re-record" in str(info.value)


def test_several_changed_parts_are_all_named() -> None:
    entry, _ = recorded()
    with pytest.raises(CassetteMismatchError, match="differs in: system_prompt, user_prompt"):
        run(CassetteRuntime([entry]).call(req(system_prompt="a", user_prompt="b"), host(), guard()))


def test_a_role_the_cassette_never_recorded_is_named() -> None:
    entry, _ = recorded()
    planner = req(context=ctx(role=Role.PLANNER))
    with pytest.raises(CassetteMismatchError, match=r"no planner calls.*it has: editor"):
        run(CassetteRuntime([entry]).call(planner, host(), guard()))


def test_an_empty_cassette_says_so() -> None:
    with pytest.raises(CassetteMismatchError, match="it has: nothing"):
        run(CassetteRuntime([]).call(req(), host(), guard()))


def test_the_closest_entry_of_the_same_role_is_the_one_compared() -> None:
    near, _ = recorded(user_prompt="near")
    far, _ = recorded(ScriptedCall.answer(ANSWER), user_prompt="far", system_prompt="other")
    runtime = CassetteRuntime([far, near])
    with pytest.raises(CassetteMismatchError) as info:
        run(runtime.call(req(user_prompt="nearly"), host(), guard()))
    assert f"({near.request_hash[:12]})" in str(info.value)
    assert "differs in: user_prompt." in str(info.value)  # only that part, not system_prompt


def test_entries_with_one_hash_replay_in_order_and_run_out() -> None:
    first = ScriptedCall.answer({"answer": "first", "confidence": 1})
    second = ScriptedCall.answer({"answer": "second", "confidence": 1})
    e1, request = recorded(first)
    e2 = CassetteEntry.for_request(request, second)
    runtime, h = CassetteRuntime([e1, e2]), host()
    outs = [run(runtime.call(request, h, guard())).output for _ in range(2)]  # type: ignore[attr-defined]
    assert outs == [{"answer": "first", "confidence": 1}, {"answer": "second", "confidence": 1}]
    with pytest.raises(CassetteMismatchError, match=r"already replayed.*more often"):
        run(runtime.call(request, h, guard()))


def test_entries_nobody_replayed_are_reported() -> None:
    used, request = recorded()
    spare, _ = recorded(ScriptedCall.answer(ANSWER), user_prompt="never asked")
    runtime = CassetteRuntime([used, spare])
    run(runtime.call(request, host(), guard()))
    assert runtime.unused() == [spare]
    with pytest.raises(CassetteMismatchError, match=r"1 recorded call.*never replayed: editor"):
        runtime.assert_all_used()


def test_a_host_that_now_decides_differently_stops_the_replay() -> None:
    """The recording allowed view_file; a policy now denies it: the replay must not carry on."""
    entry, request = recorded()
    h = host(allowlists={Role.EDITOR: frozenset()})  # view_file is no longer allowed
    with pytest.raises(ScriptDivergedError, match="expected allow, the host said deny"):
        run(CassetteRuntime([entry]).call(request, h, guard()))


def test_a_recorded_denial_that_is_now_denied_for_another_reason_stops_the_replay() -> None:
    script = ScriptedCall.using(ScriptedToolCall(name="Bash", args={"command": "ls"}), then=ANSWER)
    entry, request = recorded(script)
    (scripted,) = entry.call.turns[0].tool_calls
    assert scripted.expect_deny_code == "unknown_tool"  # what the recording saw
    assert run(CassetteRuntime([entry]).call(request, host(), guard())).stop is StopReason.COMPLETED  # type: ignore[attr-defined]

    drifted = scripted.model_copy(update={"expect_deny_code": "role_not_allowed"})
    turn = entry.call.turns[0].model_copy(update={"tool_calls": (drifted,)})
    changed = entry.model_copy(
        update={"call": entry.call.model_copy(update={"turns": (turn, entry.call.turns[1])})}
    )
    with pytest.raises(ScriptDivergedError, match="expected denial 'role_not_allowed'"):
        run(CassetteRuntime([changed]).call(request, host(), guard()))


def test_outputs_are_compared_only_in_strict_mode() -> None:
    entry, request = recorded()

    from runtime_helpers import ViewArgs, ViewOut

    from mpilot.testing.doubles import StaticTool
    from mpilot.tools.base import ToolEffect, ToolSpec

    spec = ToolSpec("view_file", "Read.", ViewArgs, ViewOut, ToolEffect.READ)
    other = StaticTool(spec, lambda a: ViewOut(content="a different file now", total_lines=9))
    h = InMemoryToolHost(tools=[other], allowlists={Role.EDITOR: frozenset({"view_file"})})
    assert run(CassetteRuntime([entry]).call(request, h, guard())).stop is StopReason.COMPLETED  # type: ignore[attr-defined]

    strict = CassetteRuntime([entry], strict_outputs=True)
    h2 = InMemoryToolHost(tools=[other], allowlists={Role.EDITOR: frozenset({"view_file"})})
    with pytest.raises(ScriptDivergedError, match="output differs"):
        run(strict.call(request, h2, guard()))


def test_the_opus_check_applies_to_replayed_results_too() -> None:
    """A cassette recorded on another model can never be replayed as if it were routed."""
    wrong = ScriptedCall.answer(ANSWER, model_id=SONNET)
    entry, request = recorded(wrong)
    result: RoleCallResult = run(CassetteRuntime([entry]).call(request, host(), guard()))
    from mpilot.core.errors import ForbiddenModelError

    with pytest.raises(ForbiddenModelError):
        check_result(request, result)
    assert result.model_id != OPUS
