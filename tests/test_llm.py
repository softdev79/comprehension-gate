"""gate/llm.py and gate/agents.py with a fake OpenAI client: no SDK, key or network needed."""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gate import agents, llm  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer", "enum": [0, 1, 2]}, "items": {"type": "array", "items": {"type": "string"}}},
    "required": ["n", "items"],
    "additionalProperties": False,
}


class BadRequest(Exception):
    status_code = 400


def reply(content, finish_reason="stop", refusal=None):
    message = SimpleNamespace(content=content, refusal=refusal, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


class FakeClient:
    """Returns scripted replies (or raises scripted exceptions) and records each request."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


ENV = {"GATE_PROVIDER": "openai", "GATE_JSON_MODE": "json_schema", "GATE_REASONING_EFFORT": "high",
       "MODEL_ID": "openai.gpt-oss-120b"}


class Base(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.dict(os.environ, ENV), mock.patch.object(llm, "load_dotenv", lambda path=None: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def complete(self, client, schema=SCHEMA):
        return llm.complete_json("sys", "user", schema, "thing", "high", client=client)


class ValidateTests(unittest.TestCase):
    def test_accepts_valid_and_rejects_missing_extra_enum_and_types(self):
        self.assertEqual(llm.validate({"n": 1, "items": ["a"]}, SCHEMA), {"n": 1, "items": ["a"]})
        for bad in ({"n": 1}, {"n": 1, "items": [], "x": 1}, {"n": 3, "items": []},
                    {"n": True, "items": []}, {"n": 1, "items": [1]}, {"n": "1", "items": []}):
            with self.assertRaises(llm.SchemaError, msg=bad):
                llm.validate(bad, SCHEMA)

    def test_agent_schemas_reject_out_of_range_scores(self):
        good = {"results": [{"id": "Q1", "score": 4, "feedback": "", "follow_up": ""}], "overall_comment": ""}
        llm.validate(good, agents.GRADE_SCHEMA)
        good["results"][0]["score"] = 5
        with self.assertRaises(llm.SchemaError):
            llm.validate(good, agents.GRADE_SCHEMA)


class ExtractTests(unittest.TestCase):
    def test_ignores_reasoning_fences_and_prose(self):
        text = '<reasoning>maybe {"n": 2}</reasoning>Here you go:\n```json\n{"n": 1, "items": []}\n```'
        self.assertEqual(llm.extract_json(text), {"n": 1, "items": []})

    def test_no_json_raises(self):
        with self.assertRaises(llm.SchemaError):
            llm.extract_json("no braces here")
        with self.assertRaises(llm.SchemaError):
            llm.extract_json(None)


class CompleteJsonTests(Base):
    def test_sends_strict_schema_and_effort(self):
        client = FakeClient(reply('{"n": 1, "items": ["a"]}'))
        self.assertEqual(self.complete(client), {"n": 1, "items": ["a"]})
        sent = client.requests[0]
        self.assertEqual(sent["model"], "openai.gpt-oss-120b")
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertTrue(sent["response_format"]["json_schema"]["strict"])
        self.assertEqual(sent["reasoning_effort"], "high")
        self.assertIn('"additionalProperties": false', sent["messages"][0]["content"])  # schema also in prompt

    def test_retries_once_with_the_error_then_succeeds(self):
        client = FakeClient(reply('{"n": 9, "items": []}'), reply('{"n": 2, "items": []}'))
        self.assertEqual(self.complete(client)["n"], 2)
        self.assertIn("rejected", client.requests[1]["messages"][-1]["content"])

    def test_gives_up_after_second_invalid_reply(self):
        client = FakeClient(reply('{"n": 9, "items": []}'), reply("still not json"))
        with self.assertRaises(RuntimeError):
            self.complete(client)

    def test_steps_down_when_endpoint_rejects_options(self):
        # 1st 400 drops reasoning_effort, 2nd 400 drops json_schema for json_object.
        client = FakeClient(BadRequest("no effort"), BadRequest("no json_schema"), reply('{"n": 0, "items": []}'))
        self.assertEqual(self.complete(client)["n"], 0)
        last = client.requests[-1]
        self.assertNotIn("reasoning_effort", last)
        self.assertEqual(last["response_format"], {"type": "json_object"})

    def test_prompt_mode_sends_no_response_format(self):
        with mock.patch.dict(os.environ, {"GATE_JSON_MODE": "prompt", "GATE_REASONING_EFFORT": ""}):
            client = FakeClient(reply('{"n": 0, "items": []}'))
            self.complete(client)
        self.assertNotIn("response_format", client.requests[0])
        self.assertNotIn("reasoning_effort", client.requests[0])

    def test_other_errors_are_not_swallowed(self):
        class ServerError(Exception):
            status_code = 500
        with self.assertRaises(ServerError):
            self.complete(FakeClient(ServerError("boom")))

    def test_refusal_and_truncation_raise(self):
        with self.assertRaisesRegex(RuntimeError, "declined"):
            self.complete(FakeClient(reply(None, refusal="no")))
        with self.assertRaisesRegex(RuntimeError, "declined"):
            self.complete(FakeClient(reply("", finish_reason="content_filter")))
        with self.assertRaisesRegex(RuntimeError, "cut off"):
            self.complete(FakeClient(reply('{"n": 1', finish_reason="length")))

    def test_unknown_provider_is_an_error(self):
        with mock.patch.dict(os.environ, {"GATE_PROVIDER": "bogus"}):
            with self.assertRaises(RuntimeError):
                self.complete(FakeClient())


class AgentTests(Base):
    def test_generate_questions_through_llm(self):
        out = {"change_summary": "adds add()", "questions": [
            {"area": "business_logic", "question": "Why?", "code_ref": "app.py:2-3"}]}
        client = FakeClient(reply(json.dumps(out)))
        with mock.patch.object(llm, "make_openai_client", lambda: client):
            self.assertEqual(agents.generate_questions("diff", {"files": []}, "low", 1), out)
        self.assertIn("<diff>\ndiff\n</diff>", client.requests[0]["messages"][1]["content"])

    def test_grader_output_with_wrong_area_or_extra_field_is_rejected(self):
        bad = {"results": [{"id": "Q1", "score": 4, "feedback": "", "follow_up": "", "passed": True}],
               "overall_comment": ""}
        client = FakeClient(reply(json.dumps(bad)), reply(json.dumps(bad)))
        q = {"questions": [{"id": "Q1", "area": "testing", "question": "?", "code_ref": "a"}]}
        with mock.patch.object(llm, "make_openai_client", lambda: client):
            with self.assertRaises(RuntimeError):
                agents.grade_answers("diff", {}, q, {"Q1": "a"})


class DotenvTests(unittest.TestCase):
    def test_reads_values_without_overriding_existing(self):
        path = os.path.join(tempfile.mkdtemp(), ".env")
        with open(path, "w") as f:
            f.write("# comment\nGATE_T_A=1\nGATE_T_B='two'\nGATE_T_C=\nnot a line\n")
        with mock.patch.dict(os.environ, {"GATE_T_A": "keep"}):
            llm.load_dotenv(path)
            self.assertEqual((os.environ["GATE_T_A"], os.environ["GATE_T_B"]), ("keep", "two"))
            self.assertNotIn("GATE_T_C", os.environ)

    def test_default_path_is_the_tool_folder_not_the_cwd(self):
        self.assertEqual(llm.TOOL_DIR, ROOT)


if __name__ == "__main__":
    unittest.main()
