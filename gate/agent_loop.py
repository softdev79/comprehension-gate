"""A bounded tool-calling loop for the OpenAI-compatible provider (Amazon Bedrock).

The model may call the read-only tools in gate/tools.py to study the change, and finishes
by calling a "submit" tool whose arguments are the answer. The answer is validated in code
(schema plus an optional semantic check); a rejected answer goes back to the model as a
tool error. Hard limits on turns, tool calls and tool output bound cost and time. The
result is still only data: gate/policy.py makes the pass/fail decision.
"""
import json
import os

from . import llm

DEFAULT_MAX_TURNS = 12
MAX_TOOL_CALLS = 40
MAX_TOTAL_TOOL_CHARS = 150000
MAX_SUBMIT_ATTEMPTS = 3


def _limit(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _assistant_message(message):
    """The assistant turn as a plain dict, so it can be sent back with the tool results."""
    out = {"role": "assistant", "content": message.content or ""}
    calls = message.tool_calls or []
    if calls:
        out["tool_calls"] = [{"id": c.id, "type": "function",
                              "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                             for c in calls]
    return out


def _parse_arguments(raw):
    try:
        value = json.loads(raw or "{}")
    except ValueError as exc:
        return None, "arguments are not valid JSON (%s)" % exc
    if not isinstance(value, dict):
        return None, "arguments must be a JSON object"
    return value, None


def run(system, user, tools, submit_name, submit_description, schema, check=None,
        effort="high", client=None, log=None):
    """Run the loop and return the validated submit arguments. Raises RuntimeError on failure.

    tools: a gate.tools.RepoTools (or anything with .call(name, args) and the same specs).
    check: optional function(result) -> list of problems; non-empty means "fix and resubmit".
    log:   optional function(str) for a trace of tool use (names and sizes only).
    """
    from . import tools as tool_module

    client = client or llm.make_openai_client()
    effort = os.environ.get("GATE_REASONING_EFFORT", effort) or None
    max_turns = _limit("GATE_AGENT_MAX_TURNS", DEFAULT_MAX_TURNS)
    log = log or (lambda text: None)

    submit_spec = {"type": "function", "function": {
        "name": submit_name, "description": submit_description, "parameters": schema}}
    specs = tool_module.openai_tool_specs() + [submit_spec]
    messages = [
        {"role": "system", "content": system + (
            "\n\nYou have read-only tools to inspect the repository. Use them to ground your work in "
            "the actual code, then call %s exactly once with your final answer. You have at most %d "
            "turns; do not repeat a call you have already made." % (submit_name, max_turns))},
        {"role": "user", "content": user},
    ]
    tool_calls = tool_chars = submit_attempts = 0

    for turn in range(max_turns + 1):
        last_turn = turn == max_turns
        kwargs = {"model": llm.openai_model(), "messages": messages, "tools": specs}
        if last_turn:  # out of turns: the only option left is to submit
            kwargs["tool_choice"] = {"type": "function", "function": {"name": submit_name}}
            messages.append({"role": "user", "content": "Turn limit reached. Call %s now with your best answer."
                                                        % submit_name})
        if effort:
            kwargs["reasoning_effort"] = effort
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            if llm._is_bad_request(exc) and effort:
                effort = None  # endpoint rejected reasoning_effort: retry without it
                kwargs.pop("reasoning_effort", None)
                response = client.chat.completions.create(**kwargs)
            else:
                raise
        choice = response.choices[0]
        message = choice.message
        if getattr(message, "refusal", None) or choice.finish_reason == "content_filter":
            raise RuntimeError("The model declined this request; the gate cannot proceed automatically.")
        if choice.finish_reason == "length":
            raise RuntimeError("The model's output was cut off; the diff may be too large for one pass.")

        calls = message.tool_calls or []
        if not calls:
            # No tool call: accept a final JSON answer in plain content, else nudge once more.
            messages.append(_assistant_message(message))
            try:
                result = llm.validate(llm.extract_json(message.content), schema)
                problems = check(result) if check else []
                if not problems:
                    log("turn %d: answer in message content" % turn)
                    return result
                messages.append({"role": "user", "content": "Rejected: %s. Call %s with a corrected answer."
                                                            % ("; ".join(problems), submit_name)})
            except llm.SchemaError:
                messages.append({"role": "user", "content": "Continue with the tools, or call %s with your "
                                                            "final answer." % submit_name})
            continue

        messages.append(_assistant_message(message))
        for call in calls:
            name = call.function.name
            args, error = _parse_arguments(call.function.arguments)
            if name == submit_name:
                submit_attempts += 1
                if error is None:
                    try:
                        llm.validate(args, schema)
                        problems = check(args) if check else []
                    except llm.SchemaError as exc:
                        problems = [str(exc)]
                    if not problems:
                        log("turn %d: submitted after %d tool calls" % (turn, tool_calls))
                        return args
                    error = "; ".join(problems)
                log("turn %d: submit rejected (%s)" % (turn, error[:200]))
                if submit_attempts >= MAX_SUBMIT_ATTEMPTS:
                    raise RuntimeError("The model's answer did not pass validation: %s" % error)
                content = "Rejected, fix and call %s again: %s" % (submit_name, error)
            elif error is not None:
                content = "error: " + error
            elif tool_calls >= MAX_TOOL_CALLS or tool_chars >= MAX_TOTAL_TOOL_CHARS:
                content = "error: tool budget used up; call %s now" % submit_name
            else:
                tool_calls += 1
                content = tools.call(name, args)
                tool_chars += len(content)
                log("turn %d: %s -> %d chars" % (turn, name, len(content)))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    raise RuntimeError("The model did not submit a valid answer within %d turns." % max_turns)
