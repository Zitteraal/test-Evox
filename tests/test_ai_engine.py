import json
import unittest
from types import SimpleNamespace
from unittest import mock

from config import settings
from core import ai_engine
from core.tool_result import ToolResult


# --------------------------------------------------------------------------- #
# Gespieltes Ollama
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, data=None, lines=None):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self._lines = lines or []

    def json(self):
        return self._data

    def read(self):
        return b""

    def iter_lines(self):
        return iter(self._lines)


class FakeStream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *exc):
        return False


def make_client(responses):
    """Erzeugt eine Fake-Client-Klasse, die die Antworten der Reihe nach liefert."""
    queue = list(responses)
    payloads = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def _next(self):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        def post(self, url, json=None):
            payloads.append(json)
            return self._next()

        def stream(self, method, url, json=None):
            payloads.append(json)
            return FakeStream(self._next())

    FakeClient.payloads = payloads
    return FakeClient


def reply(content="", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return FakeResponse(200, {"message": message})


def call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


class FakeToolManager:
    def __init__(self, result=None, needs_confirmation=False, error=None):
        self.result = result or ToolResult.success("done", verified=True)
        self.needs_confirmation = needs_confirmation
        self.error = error
        self.executed = []

    def prepare_call(self, name, args):
        return SimpleNamespace(
            error=self.error, args=args, needs_confirmation=self.needs_confirmation,
            description=f"[HIGH RISK] {name}", risk=3,
        )

    def execute_tool(self, name, args, confirmed=False):
        self.executed.append((name, args, confirmed))
        return self.result


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = (settings.MAX_TOOL_ROUNDS, settings.CONFIRM_TIMEOUT_SECONDS,
                       settings.STREAM_RESPONSES, settings.RESPONSE_LANGUAGE)
        self.addCleanup(self._restore)

    def _restore(self):
        (settings.MAX_TOOL_ROUNDS, settings.CONFIRM_TIMEOUT_SECONDS,
         settings.STREAM_RESPONSES, settings.RESPONSE_LANGUAGE) = self._saved

    def run_worker(self, responses, tm=None, on_confirm=None, tools=None):
        client_cls = make_client(responses)
        worker = ai_engine.ChatWorker(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
            "test-model", tools=tools or [{"type": "function"}], tool_manager=tm or FakeToolManager(),
        )
        out = SimpleNamespace(chunks=[], finished=[], failed=[], confirm=[], cancelled=[], system=[],
                              worker=worker, payloads=client_cls.payloads)
        worker.chunk.connect(out.chunks.append)
        worker.finished.connect(out.finished.append)
        worker.failed.connect(out.failed.append)
        worker.confirmRequired.connect(lambda n, d: out.confirm.append((n, d)))
        worker.confirmCancelled.connect(lambda: out.cancelled.append(True))
        worker.systemMessage.connect(out.system.append)
        if on_confirm:
            worker.confirmRequired.connect(lambda n, d: on_confirm(worker))
        with mock.patch.object(ai_engine.httpx, "Client", client_cls):
            worker.run()
        return out


class PlainAnswerTests(EngineTestCase):
    def test_plain_answer(self):
        out = self.run_worker([reply("Hallo!")])
        self.assertEqual(out.finished, ["Hallo!"])
        self.assertEqual(out.chunks, ["Hallo!"])
        self.assertEqual(out.failed, [])

    def test_empty_answer_without_tools_is_reported_honestly(self):
        out = self.run_worker([reply("")])
        self.assertIn("keine Antwort", out.finished[0])
        self.assertNotIn("Sir", out.finished[0])

    def test_http_error_includes_detail(self):
        out = self.run_worker([FakeResponse(404, {"error": "model 'x' not found"})])
        self.assertEqual(out.finished, [])
        self.assertEqual(len(out.failed), 1)
        self.assertIn("HTTP 404", out.failed[0])
        self.assertIn("not found", out.failed[0])

    def test_exception_is_reported_as_failure(self):
        def boom(*a, **k):
            raise RuntimeError("connection refused")

        with mock.patch.object(ai_engine.httpx, "Client", boom):
            worker = ai_engine.ChatWorker([], "m", tool_manager=FakeToolManager())
            failed = []
            worker.failed.connect(failed.append)
            worker.run()
        self.assertIn("connection refused", failed[0])


class NoFakeSuccessTests(EngineTestCase):
    def test_failed_tool_with_empty_model_answer_is_not_reported_as_success(self):
        tm = FakeToolManager(result=ToolResult.failure("Program not found"))
        out = self.run_worker([reply("", [call("open_application", {"app_name": "x"})]), reply("")], tm=tm)
        text = out.finished[0]
        self.assertIn("0 von 1", text)
        self.assertIn("1 fehlgeschlagen", text)
        self.assertIn("Program not found", text)
        self.assertNotIn("successfully", text.lower())
        self.assertNotIn("Sir", text)

    def test_verified_success_summary(self):
        out = self.run_worker([reply("", [call("t", {})]), reply("")])
        self.assertIn("1 von 1", out.finished[0])
        self.assertIn("1 davon geprüft", out.finished[0])

    def test_unverified_result_is_called_out(self):
        tm = FakeToolManager(result=ToolResult.success("sent", verified=False))
        out = self.run_worker([reply("", [call("t", {})]), reply("")], tm=tm)
        self.assertIn("0 davon geprüft", out.finished[0])
        self.assertIn("nicht bestätigen", out.finished[0])

    def test_english_language_setting_switches_fallback_text(self):
        settings.RESPONSE_LANGUAGE = "en"
        tm = FakeToolManager(result=ToolResult.failure("nope"))
        out = self.run_worker([reply("", [call("t", {})]), reply("")], tm=tm)
        self.assertIn("1 failed", out.finished[0])

    def test_model_answer_is_kept_when_present(self):
        out = self.run_worker([reply("", [call("t", {})]), reply("Fertig, die Datei wurde angelegt.")])
        self.assertEqual(out.finished, ["Fertig, die Datei wurde angelegt."])

    def test_tool_result_is_sent_to_model_with_status_and_tool_name(self):
        tm = FakeToolManager(result=ToolResult.failure("denied"))
        out = self.run_worker([reply("", [call("open_application", {"app_name": "x"})]), reply("ok")], tm=tm)
        second_request = out.payloads[1]["messages"]
        tool_msgs = [m for m in second_request if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_name"], "open_application")
        self.assertTrue(tool_msgs[0]["content"].startswith("FAILED:"))

    def test_chat_shows_status_icons(self):
        tm = FakeToolManager(result=ToolResult.failure("denied"))
        out = self.run_worker([reply("", [call("t", {})]), reply("ok")], tm=tm)
        self.assertTrue(out.system[0].startswith("✘"))


class RoundLimitTests(EngineTestCase):
    def test_exhausted_rounds_are_reported_not_hidden(self):
        settings.MAX_TOOL_ROUNDS = 3
        tm = FakeToolManager()
        out = self.run_worker([reply("", [call("t", {})])], tm=tm)  # Modell hört nie auf
        self.assertEqual(len(tm.executed), 3)
        self.assertIn("3 Tool-Runden", out.finished[0])
        self.assertNotIn("successfully", out.finished[0].lower())

    def test_exhausted_rounds_with_no_results(self):
        settings.MAX_TOOL_ROUNDS = 1
        tm = FakeToolManager(error="bad call")
        out = self.run_worker([reply("", [call("t", {})])], tm=tm)
        self.assertIn("1 Tool-Runden", out.finished[0])


class ArgumentTests(EngineTestCase):
    def test_invalid_json_arguments_are_not_executed(self):
        tm = FakeToolManager()
        out = self.run_worker([reply("", [call("write_code_file", "{not json")]), reply("ok")], tm=tm)
        self.assertEqual(tm.executed, [])
        tool_msg = [m for m in out.payloads[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("not valid JSON", tool_msg["content"])

    def test_json_string_arguments_are_parsed(self):
        tm = FakeToolManager()
        self.run_worker([reply("", [call("t", json.dumps({"a": 1}))]), reply("ok")], tm=tm)
        self.assertEqual(tm.executed[0][1], {"a": 1})

    def test_non_object_arguments_are_rejected(self):
        tm = FakeToolManager()
        self.run_worker([reply("", [call("t", "[1,2]")]), reply("ok")], tm=tm)
        self.assertEqual(tm.executed, [])

    def test_prepare_error_is_reported_and_not_executed(self):
        tm = FakeToolManager(error="outside allowed folders")
        out = self.run_worker([reply("", [call("t", {})]), reply("ok")], tm=tm)
        self.assertEqual(tm.executed, [])
        tool_msg = [m for m in out.payloads[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("outside allowed folders", tool_msg["content"])

    def test_missing_arguments_default_to_empty_dict(self):
        tm = FakeToolManager()
        self.run_worker([reply("", [{"function": {"name": "analyze_disk"}}]), reply("ok")], tm=tm)
        self.assertEqual(tm.executed[0][1], {})


class ConfirmationTests(EngineTestCase):
    def test_denied_action_is_not_executed(self):
        tm = FakeToolManager(needs_confirmation=True)
        out = self.run_worker([reply("", [call("delete_project", {"project_name": "x"})]), reply("ok")],
                              tm=tm, on_confirm=lambda w: w.set_confirmation(False))
        self.assertEqual(tm.executed, [])
        self.assertEqual(out.confirm[0][0], "delete_project")
        self.assertIn("HIGH RISK", out.confirm[0][1])
        tool_msg = [m for m in out.payloads[1]["messages"] if m["role"] == "tool"][0]
        self.assertTrue(tool_msg["content"].startswith("FAILED:"))
        self.assertIn("cancelled", tool_msg["content"])

    def test_approved_action_is_executed_with_confirmed_flag(self):
        tm = FakeToolManager(needs_confirmation=True)
        self.run_worker([reply("", [call("delete_project", {"project_name": "x"})]), reply("ok")],
                        tm=tm, on_confirm=lambda w: w.set_confirmation(True))
        self.assertEqual(len(tm.executed), 1)
        self.assertTrue(tm.executed[0][2])

    def test_no_answer_in_time_counts_as_denied_and_closes_popup(self):
        settings.CONFIRM_TIMEOUT_SECONDS = 0.05
        tm = FakeToolManager(needs_confirmation=True)
        out = self.run_worker([reply("", [call("delete_project", {"project_name": "x"})]), reply("ok")], tm=tm)
        self.assertEqual(tm.executed, [])
        self.assertEqual(out.cancelled, [True])

    def test_low_risk_tools_do_not_ask(self):
        tm = FakeToolManager(needs_confirmation=False)
        out = self.run_worker([reply("", [call("list_directory", {"path": "x"})]), reply("ok")], tm=tm)
        self.assertEqual(out.confirm, [])
        self.assertFalse(tm.executed[0][2])

    def test_each_high_risk_call_asks_separately(self):
        tm = FakeToolManager(needs_confirmation=True)
        answers = iter([True, False])
        out = self.run_worker(
            [reply("", [call("delete_project", {"project_name": "a"}), call("delete_project", {"project_name": "b"})]),
             reply("ok")],
            tm=tm, on_confirm=lambda w: w.set_confirmation(next(answers)),
        )
        self.assertEqual(len(out.confirm), 2)
        self.assertEqual([c[1]["project_name"] for c in tm.executed], ["a"])


class StreamingTests(EngineTestCase):
    def stream_reply(self, pieces, tool_calls=None):
        lines = []
        for piece in pieces:
            lines.append(json.dumps({"message": {"role": "assistant", "content": piece}, "done": False}))
        if tool_calls:
            lines.append(json.dumps({"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}, "done": False}))
        lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}))
        return FakeResponse(200, lines=lines)

    def test_streamed_answer_is_emitted_in_pieces_without_duplicate(self):
        settings.STREAM_RESPONSES = True
        out = self.run_worker([self.stream_reply(["Hal", "lo"])])
        self.assertEqual(out.chunks, ["Hal", "lo"])
        self.assertEqual(out.finished, ["Hallo"])

    def test_streamed_tool_call_then_answer(self):
        settings.STREAM_RESPONSES = True
        tm = FakeToolManager()
        out = self.run_worker([self.stream_reply([], [call("t", {"x": 1})]), self.stream_reply(["Fer", "tig"])], tm=tm)
        self.assertEqual(tm.executed[0][1], {"x": 1})
        self.assertEqual(out.finished, ["Fertig"])

    def test_streamed_error_line(self):
        settings.STREAM_RESPONSES = True
        out = self.run_worker([FakeResponse(200, lines=[json.dumps({"error": "out of memory"})])])
        self.assertIn("out of memory", out.failed[0])
        self.assertEqual(out.finished, [])

    def test_streamed_http_error(self):
        settings.STREAM_RESPONSES = True
        out = self.run_worker([FakeResponse(500, {"error": "server down"})])
        self.assertIn("HTTP 500", out.failed[0])

    def test_garbage_lines_are_skipped(self):
        settings.STREAM_RESPONSES = True
        lines = ["not json", "", json.dumps({"message": {"content": "ok"}, "done": True})]
        out = self.run_worker([FakeResponse(200, lines=lines)])
        self.assertEqual(out.finished, ["ok"])


if __name__ == "__main__":
    unittest.main()
