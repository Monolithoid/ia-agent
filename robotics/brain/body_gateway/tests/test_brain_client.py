"""Tests sans ROS 2 ni réseau du client LLM (construction des requêtes par fournisseur).

    cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest
from unittest import mock

import httpx2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain_client  # noqa: E402

MESSAGES = [
    {"role": "system", "content": "Réponds en JSON."},
    {"role": "user",   "content": "ok nao, bonjour"},
]
ANSWER = '{"emotion": "joy", "actions": []}'


class _FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self._lines)


def _sse(*contents):
    lines = [
        b"data: " + json.dumps({"choices": [{"delta": {"content": c}}]}).encode()
        for c in contents
    ]
    return lines + [b"data: [DONE]"]


class TestFromEnv(unittest.TestCase):

    def test_default_is_ollama_unchanged(self):
        brain = brain_client.BrainClient.from_env({})
        self.assertEqual(brain.provider, "ollama")
        self.assertEqual(brain.url, "http://localhost:11434")
        self.assertEqual(brain.model, "qwen3:1.7b")

    def test_empty_values_fall_back_to_provider_defaults(self):
        # docker-compose passes BRAIN_URL= / BRAIN_MODEL= when they are not set.
        brain = brain_client.BrainClient.from_env(
            {"BRAIN_PROVIDER": "anthropic", "BRAIN_URL": "", "BRAIN_MODEL": "", "BRAIN_API_KEY": "k"}
        )
        self.assertEqual(brain.model, "claude-haiku-4-5")
        self.assertEqual(brain.url, "")

    def test_openai_defaults(self):
        brain = brain_client.BrainClient.from_env({"BRAIN_PROVIDER": "OpenAI"})
        self.assertEqual(brain.url, "https://api.openai.com")

    def test_unknown_provider(self):
        with self.assertRaises(ValueError):
            brain_client.BrainClient.from_env({"BRAIN_PROVIDER": "mistral"})


class TestOpenAICompatible(unittest.TestCase):

    def _chat(self, provider):
        brain = brain_client.BrainClient(provider=provider, api_key="secret", timeout=60)
        with mock.patch.object(brain_client.requests, "post",
                               return_value=_FakeResponse(_sse('{"emotion": ', '"joy", "actions": []}'))) as post:
            text = brain.chat(MESSAGES, 0.7)
        return text, post

    def test_ollama_request_unchanged(self):
        text, post = self._chat("ollama")
        self.assertEqual(text, ANSWER)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://localhost:11434/v1/chat/completions")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer secret"})
        self.assertEqual(kwargs["timeout"], (10, 60))
        self.assertEqual(kwargs["json"], {
            "model": "qwen3:1.7b", "messages": MESSAGES, "stream": True,
            "temperature": 0.7, "options": {"think": False},
        })

    def test_openai_has_no_options(self):
        _, post = self._chat("openai")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.openai.com/v1/chat/completions")
        self.assertNotIn("options", kwargs["json"])
        self.assertEqual(kwargs["json"]["temperature"], 0.7)

    def test_chunk_without_choices_is_ignored(self):
        lines = [b'data: {"choices": []}'] + _sse(ANSWER)
        brain = brain_client.BrainClient(provider="openai")
        with mock.patch.object(brain_client.requests, "post", return_value=_FakeResponse(lines)):
            self.assertEqual(brain.chat(MESSAGES, 0.5), ANSWER)


class TestAnthropicRequest(unittest.TestCase):

    def test_haiku_4_5_request(self):
        kwargs = brain_client.build_anthropic_request("claude-haiku-4-5", MESSAGES, 0.5)
        self.assertEqual(kwargs["system"], "Réponds en JSON.")
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "ok nao, bonjour"}])
        self.assertEqual(kwargs["extra_body"], {"temperature": 0.5})
        self.assertNotIn("thinking", kwargs)
        self.assertNotIn("output_config", kwargs)

    def test_no_temperature_for_models_that_reject_it(self):
        for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                      "claude-opus-4-8", "claude-opus-4-7", "claude-fable-5-1"):
            kwargs = brain_client.build_anthropic_request(model, MESSAGES, 0.7)
            self.assertNotIn("extra_body", kwargs, model)

    def test_effort_only_when_set(self):
        kwargs = brain_client.build_anthropic_request("claude-opus-5-5", MESSAGES, 0.7, "low")
        self.assertEqual(kwargs["output_config"], {"effort": "low"})


def _anthropic_sse(text, stop_reason="end_turn"):
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 12}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


class TestAnthropicChat(unittest.TestCase):
    """Full SDK round trip against a mocked HTTP transport (no network)."""

    def _chat(self, model, stop_reason="end_turn"):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                   content=_anthropic_sse(ANSWER, stop_reason).encode())

        brain = brain_client.BrainClient(provider="anthropic", model=model, api_key="sk-test")
        brain._anthropic = brain._anthropic.with_options(
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        return brain.chat(MESSAGES, 0.7), seen

    def test_haiku_round_trip(self):
        text, seen = self._chat("claude-haiku-4-5")
        self.assertEqual(text, ANSWER)
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], "sk-test")
        body = seen["body"]
        self.assertEqual(body["model"], "claude-haiku-4-5")
        self.assertTrue(body["stream"])
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["system"], "Réponds en JSON.")
        self.assertEqual(body["messages"], [{"role": "user", "content": "ok nao, bonjour"}])

    def test_opus_5_5_sends_no_temperature(self):
        _, seen = self._chat("claude-opus-5-5")
        self.assertNotIn("temperature", seen["body"])

    def test_refusal_raises(self):
        with self.assertRaises(RuntimeError):
            self._chat("claude-haiku-4-5", stop_reason="refusal")


if __name__ == "__main__":
    unittest.main()
