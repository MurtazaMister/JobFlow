"""Tests for the local llama.cpp (Gemma) scorer — HTTP mocked, no model needed."""

import json

import pytest

from jobflow import ai_llama, ai_local
from jobflow.ai_prompt import BATCH_PROMPT


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ai_llama.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _batch(n, desc="Build backend services."):
    return [(f"https://x.com/{i}", "Acme", f"SWE {i}", "NYC", desc, None) for i in range(n)]


def test_score_batch_sends_grammar_and_parses(monkeypatch):
    sent = {}
    scores = [{"id": i + 1, "score": 7, "reason": "ok"} for i in range(3)]

    def fake_post(url, json=None, timeout=None):
        sent.update(url=url, body=json)
        return _Resp({"choices": [{"message": {"content": __import__("json").dumps(scores)}}]})

    monkeypatch.setattr(ai_llama.requests, "post", fake_post)
    monkeypatch.setenv("LLAMA_MODEL_PATH", "")
    out = ai_llama.score_batch_with_llama(_batch(3), "profile")

    assert out == scores
    body = sent["body"]
    assert sent["url"].endswith("/v1/chat/completions")
    assert body["temperature"] == 0
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["minItems"] == schema["maxItems"] == 3
    assert body["chat_template_kwargs"] == {"enable_thinking": False}   # default model is Gemma


def test_score_batch_caps_descriptions(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["prompt"] = json["messages"][0]["content"]
        return _Resp({"choices": [{"message": {"content": '[{"id":1,"score":5,"reason":"x"}]'}}]})

    monkeypatch.setattr(ai_llama.requests, "post", fake_post)
    ai_llama.score_batch_with_llama(_batch(1, desc="A" * 9000), "p")
    assert "A" * ai_llama.DESC_CAP in sent["prompt"]
    assert "A" * (ai_llama.DESC_CAP + 1) not in sent["prompt"]


def test_score_batch_returns_none_when_server_unreachable(monkeypatch, capsys):
    def boom(*a, **k):
        raise ai_llama.requests.ConnectionError("refused")

    monkeypatch.setattr(ai_llama.requests, "post", boom)
    assert ai_llama.score_batch_with_llama(_batch(2), "p") is None
    assert "Batch error" in capsys.readouterr().out


def test_missing_setup_names_the_problem(monkeypatch, tmp_path):
    monkeypatch.setenv("LLAMA_SERVER_PATH", str(tmp_path / "nope.exe"))
    assert "llama-server" in ai_llama.missing_setup()

    server = tmp_path / "llama-server.exe"
    server.write_text("")
    monkeypatch.setenv("LLAMA_SERVER_PATH", str(server))
    monkeypatch.setenv("LLAMA_MODEL_PATH", str(tmp_path / "missing.gguf"))
    assert "Model file" in ai_llama.missing_setup()

    model = tmp_path / "m.gguf"
    model.write_text("")
    monkeypatch.setenv("LLAMA_MODEL_PATH", str(model))
    assert ai_llama.missing_setup() is None


def test_llama_server_reuses_a_running_server(monkeypatch):
    monkeypatch.setattr(ai_llama, "_healthy", lambda: True)
    monkeypatch.setattr(ai_llama.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not start a second server"))
    with ai_llama.LlamaServer() as srv:
        assert srv.proc is None


def test_engine_dispatch_and_batch_sizes(monkeypatch):
    called = {}
    monkeypatch.setattr(ai_llama, "score_batch_with_llama",
                        lambda batch, profile: called.setdefault("gemma", len(batch)) and [])
    ai_local.score_batch(_batch(2), "p", "gemma")
    assert called == {"gemma": 2}
    assert ai_local.DEFAULT_ENGINE == "gemma"
    assert ai_local.ENGINES[0] == "gemma"
    assert ai_local.batch_size("gemma") == ai_llama.BATCH_SIZE
    assert ai_local.batch_size("claude") == 15


def test_prompt_forbids_zero_for_missing_description():
    prompt = BATCH_PROMPT.format(profile="p", jobs_block="j")
    assert "MISSING OR THIN DESCRIPTIONS" in prompt
    assert "NEVER a reason for 0" in prompt
    json.loads('[{"id": 1, "score": 5, "reason": "x"}]')  # sanity: the example shape is valid JSON
