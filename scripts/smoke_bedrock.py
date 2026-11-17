"""Check what the configured OpenAI-compatible endpoint (e.g. Amazon Bedrock) supports.

    python scripts/smoke_bedrock.py

Reads OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_PROJECT_ID and MODEL_ID from the environment
or this project's .env. Prints PASS/FAIL per feature and never prints the key. Each check is
one small request, so the whole run costs a fraction of a cent.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gate import llm  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}, "unit": {"type": "string", "enum": ["apples"]}},
    "required": ["answer", "unit"],
    "additionalProperties": False,
}
TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from the repository.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"], "additionalProperties": False},
    },
}
PROMPT = [{"role": "user", "content": "I have 2 apples and buy 3 more. Reply as JSON with fields answer and unit."}]


def check(name, fn):
    try:
        detail = fn()
        print("PASS  %-24s %s" % (name, detail or ""))
        return True
    except Exception as exc:  # report and keep going
        status = getattr(exc, "status_code", "")
        print("FAIL  %-24s %s %s" % (name, status, str(exc).splitlines()[0][:200]))
        return False


def main():
    llm.load_dotenv()
    model = llm.openai_model()
    print("Endpoint: %s" % os.environ.get("OPENAI_BASE_URL", "(not set)"))
    print("Model:    %s" % model)
    print("Project:  %s\n" % ("set" if os.environ.get("OPENAI_PROJECT_ID") else "not set"))
    try:
        client = llm.make_openai_client()
    except Exception as exc:
        print("Cannot create the client: %s" % exc)
        return 2

    def create(**extra):
        return client.chat.completions.create(model=model, **extra)

    def plain():
        r = create(messages=[{"role": "user", "content": "Reply with the single word: ok"}])
        return repr((r.choices[0].message.content or "").strip()[:40])

    def json_schema():
        r = create(messages=PROMPT, response_format={
            "type": "json_schema", "json_schema": {"name": "apples", "schema": SCHEMA, "strict": True}})
        return json.dumps(llm.validate(llm.extract_json(r.choices[0].message.content), SCHEMA))

    def json_object():
        r = create(messages=PROMPT, response_format={"type": "json_object"})
        return json.dumps(llm.extract_json(r.choices[0].message.content))

    def effort():
        r = create(messages=[{"role": "user", "content": "Reply: ok"}], reasoning_effort="high")
        return "finish_reason=%s" % r.choices[0].finish_reason

    def tools():
        r = create(messages=[{"role": "user", "content": "Read the file src/app.py using the tool."}],
                   tools=[TOOL], tool_choice="auto")
        calls = r.choices[0].message.tool_calls or []
        if not calls:
            raise RuntimeError("no tool call returned (finish_reason=%s)" % r.choices[0].finish_reason)
        return "%s(%s)" % (calls[0].function.name, calls[0].function.arguments)

    def gate_path():
        return json.dumps(llm.complete_json("You answer arithmetic questions.", PROMPT[0]["content"],
                                            SCHEMA, "apples", client=client))

    results = [
        check("plain chat", plain),
        check("json_schema (strict)", json_schema),
        check("json_object", json_object),
        check("reasoning_effort", effort),
        check("tool calling", tools),
        check("gate.llm.complete_json", gate_path),
    ]
    print("\nRequired for Phase 1: plain chat and gate.llm.complete_json. Phase 2 also needs tool calling.")
    return 0 if results[0] and results[-1] else 1


if __name__ == "__main__":
    sys.exit(main())
