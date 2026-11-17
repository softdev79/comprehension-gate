"""Model access for the agents: one function, complete_json, behind two providers.

- "openai": any OpenAI-compatible Chat Completions endpoint, such as Amazon Bedrock's
  (OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_PROJECT_ID, MODEL_ID).
- "anthropic": the Claude Messages API (ANTHROPIC_API_KEY, COMPREHENSION_GATE_MODEL).

Whatever the provider, the reply is parsed and checked against the JSON schema here, in
code. The model's output is untrusted: a reply that does not match the schema is retried
once with the error, then rejected. Only this module imports a model SDK, and only inside
functions, so evidence/policy/questionnaire work without one installed.
"""
import json
import os
import re

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_MODES = ("json_schema", "json_object", "prompt")


# --------------------------------------------------------------------------- settings

def load_dotenv(path=None):
    """Read KEY=VALUE lines from this tool's .env into os.environ without overriding.

    Only the tool's own folder is read, never the repository being gated: that repository
    is pull request content and must not be able to change where answers are sent.
    """
    path = path or os.path.join(TOOL_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and value and key not in os.environ:
                os.environ[key] = value


def provider():
    return os.environ.get("GATE_PROVIDER", "openai").strip().lower()


def openai_model():
    return os.environ.get("MODEL_ID", "openai.gpt-oss-120b")


def anthropic_model():
    return os.environ.get("COMPREHENSION_GATE_MODEL", "claude-opus-5-5")


# --------------------------------------------------------------------------- validation

class SchemaError(ValueError):
    pass


_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}


def validate(value, schema, where="$"):
    """Check value against the JSON-schema subset the agents use. Raises SchemaError."""
    expected = schema.get("type")
    if expected:
        py = _TYPES[expected]
        bad_bool = isinstance(value, bool) and expected in ("integer", "number")
        if not isinstance(value, py) or bad_bool:
            raise SchemaError("%s: expected %s, got %s" % (where, expected, type(value).__name__))
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError("%s: %r is not one of %r" % (where, value, schema["enum"]))
    if expected == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError("%s: missing required field %r" % (where, key))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise SchemaError("%s: unexpected field(s) %s" % (where, ", ".join(extra)))
        for key, sub in props.items():
            if key in value:
                validate(value[key], sub, "%s.%s" % (where, key))
    if expected == "array" and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], "%s[%d]" % (where, i))
    return value


_REASONING = re.compile(r"<(reasoning|think|thinking)>.*?</\1>", re.S | re.I)


def extract_json(text):
    """Return the first JSON object in a model reply, ignoring reasoning tags and fences."""
    if text is None:
        raise SchemaError("empty reply")
    text = _REASONING.sub("", text)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise SchemaError("no JSON object found in the reply")


# --------------------------------------------------------------------------- providers

def make_openai_client():
    import openai  # imported here so the rest of the gate works without the SDK

    load_dotenv()
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
        if not os.environ.get(key):
            raise RuntimeError("%s is not set (see .env.example)." % key)
    kwargs = {"api_key": os.environ["OPENAI_API_KEY"], "base_url": os.environ["OPENAI_BASE_URL"],
              "timeout": 600.0, "max_retries": 3}
    if os.environ.get("OPENAI_PROJECT_ID"):
        kwargs["project"] = os.environ["OPENAI_PROJECT_ID"]
    return openai.OpenAI(**kwargs)


def _is_bad_request(exc):
    return getattr(exc, "status_code", None) in (400, 422)


def _schema_instruction(name, schema):
    return ("\n\nReply with a single JSON object named %s and nothing else. It must match this "
            "JSON schema exactly (no extra fields):\n%s" % (name, json.dumps(schema)))


def _openai_once(client, messages, name, schema, mode, effort):
    kwargs = {"model": openai_model(), "messages": messages}
    if mode == "json_schema":
        kwargs["response_format"] = {"type": "json_schema",
                                     "json_schema": {"name": name, "schema": schema, "strict": True}}
    elif mode == "json_object":
        kwargs["response_format"] = {"type": "json_object"}
    if effort:
        kwargs["reasoning_effort"] = effort
    response = client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    message = choice.message
    if getattr(message, "refusal", None) or choice.finish_reason == "content_filter":
        raise RuntimeError("The model declined this request; the gate cannot proceed automatically.")
    if choice.finish_reason == "length":
        raise RuntimeError("The model's output was cut off; the diff may be too large for one pass.")
    return message.content or ""


def _complete_openai(system, user, schema, name, effort, client=None):
    client = client or make_openai_client()
    effort = os.environ.get("GATE_REASONING_EFFORT", effort) or None
    mode = os.environ.get("GATE_JSON_MODE", "json_schema")
    if mode not in JSON_MODES:
        raise RuntimeError("GATE_JSON_MODE must be one of %s" % ", ".join(JSON_MODES))
    # The schema is always in the prompt too: some endpoints accept response_format
    # but do not enforce it, and validation below is what the gate relies on.
    messages = [{"role": "system", "content": system + _schema_instruction(name, schema)},
                {"role": "user", "content": user}]

    modes = list(JSON_MODES[JSON_MODES.index(mode):])
    while True:  # step down json_schema -> json_object -> prompt if the endpoint rejects one
        try:
            text = _openai_once(client, messages, name, schema, modes[0], effort)
            break
        except Exception as exc:
            if _is_bad_request(exc) and effort:
                effort = None  # reasoning_effort may be unsupported: retry the same mode without it
                continue
            if _is_bad_request(exc) and len(modes) > 1:
                modes.pop(0)
                continue
            raise

    try:
        return validate(extract_json(text), schema)
    except SchemaError as first:
        retry = messages + [{"role": "assistant", "content": text},
                            {"role": "user", "content": "That reply was rejected: %s. Send the corrected "
                                                        "JSON object only." % first}]
        text = _openai_once(client, retry, name, schema, modes[0], effort)
        try:
            return validate(extract_json(text), schema)
        except SchemaError as second:
            raise RuntimeError("The model's reply did not match the %s schema: %s" % (name, second))


def _complete_anthropic(system, user, schema, effort):
    import anthropic  # imported here so the rest of the gate works without the SDK

    load_dotenv()
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=anthropic_model(),
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # on a safety decline, the API retries on a fallback model
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("The model declined this request; the gate cannot proceed automatically.")
    if message.stop_reason == "max_tokens":
        raise RuntimeError("The model's output was cut off; the diff may be too large for one pass.")
    text = next(block.text for block in message.content if block.type == "text")
    return validate(json.loads(text), schema)


def complete_json(system, user, schema, name, effort="high", client=None):
    """One request that must return a JSON object matching schema. Raises RuntimeError otherwise."""
    load_dotenv()
    which = provider()
    if which == "openai":
        return _complete_openai(system, user, schema, name, effort, client)
    if which == "anthropic":
        return _complete_anthropic(system, user, schema, effort)
    raise RuntimeError("GATE_PROVIDER must be 'openai' or 'anthropic', not %r" % which)
