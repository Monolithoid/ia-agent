"""Client LLM du cerveau : un seul point d'entrée pour Ollama, OpenAI et Anthropic.

Sans dépendance ROS 2, pour être testable seul (tests/test_brain_client.py).

  BRAIN_PROVIDER=ollama     POST {BRAIN_URL}/v1/chat/completions en streaming,
                            avec "options": {"think": False} (propre à Ollama)
  BRAIN_PROVIDER=openai     même endpoint, sans le champ "options"
  BRAIN_PROVIDER=anthropic  SDK officiel `anthropic`, messages.stream()

Le cerveau doit renvoyer du JSON strict : le texte complet est renvoyé tel quel,
le parsing reste dans body_gateway._parse_json().
"""

import json

import anthropic
import requests

PROVIDERS = ("ollama", "openai", "anthropic")

DEFAULT_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com",
    "anthropic": "",  # vide : URL par défaut du SDK (https://api.anthropic.com)
}
DEFAULT_MODELS = {
    "ollama": "qwen3:1.7b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}

# The JSON answer is short; the margin covers models whose thinking cannot be
# disabled (Opus 5.5, Sonnet 5.5) since thinking tokens count against max_tokens.
ANTHROPIC_MAX_TOKENS = 8192

# Claude models that still accept `temperature`. Opus 4.7 and later return a 400
# for any value; Sonnet 5 / 5.5 and Haiku 5.5 reject non-default values. An
# unknown (newer) model gets no temperature: the API default is used.
_TEMPERATURE_MODELS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4-0", "claude-sonnet-4-2025",
    "claude-opus-4-6", "claude-opus-4-5", "claude-opus-4-1", "claude-opus-4-0", "claude-opus-4-2025",
)

# Network/API errors of every provider: body_gateway falls back on them.
BRAIN_ERRORS = (requests.RequestException, anthropic.APIError)


def accepts_temperature(model: str) -> bool:
    return model.startswith(_TEMPERATURE_MODELS)


def build_openai_request(provider: str, model: str, messages: list, temperature: float) -> dict:
    """JSON body of /v1/chat/completions (Ollama or OpenAI)."""
    body = {
        "model":       model,
        "messages":    messages,
        "stream":      True,
        "temperature": temperature,
    }
    # Ollama-only: disables the reasoning phase of thinking models (Qwen3...).
    if provider == "ollama":
        body["options"] = {"think": False}
    return body


def build_anthropic_request(model: str, messages: list, temperature: float,
                            effort: str = "") -> dict:
    """Keyword arguments of client.messages.stream().

    The OpenAI-style "system" messages become the top-level `system` field.
    `thinking` is never sent: Haiku 4.5 then runs without thinking, and the 5.x
    models run adaptive thinking (Opus 5.5 / Sonnet 5.5 reject "disabled").
    """
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    kwargs = {
        "model":      model,
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "messages":   [
            {"role": m["role"], "content": m["content"]}
            for m in messages if m["role"] != "system"
        ],
    }
    if system:
        kwargs["system"] = system
    # SDK 1.x has no `temperature` argument any more: it goes through extra_body.
    if accepts_temperature(model):
        kwargs["extra_body"] = {"temperature": temperature}
    # Effort is not accepted by Haiku 4.5: only sent when BRAIN_EFFORT is set.
    if effort:
        kwargs["output_config"] = {"effort": effort}
    return kwargs


class BrainClient:

    def __init__(self, provider: str = "ollama", url: str = "", model: str = "",
                 api_key: str = "", timeout: int = 120, effort: str = ""):
        if provider not in PROVIDERS:
            raise ValueError(f"BRAIN_PROVIDER inconnu : {provider!r} (attendu : {', '.join(PROVIDERS)})")
        self.provider = provider
        self.url = (url or DEFAULT_URLS[provider]).rstrip("/")
        self.model = model or DEFAULT_MODELS[provider]
        self.api_key = api_key
        self.timeout = timeout
        self.effort = effort
        self._anthropic = None
        if provider == "anthropic":
            # Empty api_key: the SDK reads ANTHROPIC_API_KEY.
            self._anthropic = anthropic.Anthropic(
                api_key=api_key or None,
                base_url=self.url or None,
                # 10 s to connect, `timeout` s maximum between two chunks.
                timeout=anthropic.Timeout(timeout, connect=10.0),
            )

    @classmethod
    def from_env(cls, env) -> "BrainClient":
        provider = env.get("BRAIN_PROVIDER", "").strip().lower() or "ollama"
        return cls(
            provider=provider,
            url=env.get("BRAIN_URL", ""),
            model=env.get("BRAIN_MODEL", ""),
            api_key=env.get("BRAIN_API_KEY", ""),
            timeout=int(env.get("BRAIN_TIMEOUT", "120")),
            effort=env.get("BRAIN_EFFORT", "").strip().lower(),
        )

    def chat(self, messages: list, temperature: float) -> str:
        """Stream the LLM and return the full response text."""
        if self.provider == "anthropic":
            return self._anthropic_chat(messages, temperature)
        return self._openai_chat(messages, temperature)

    def _openai_chat(self, messages: list, temperature: float) -> str:
        """timeout=(10, timeout): 10 s to establish the connection, `timeout` s
        maximum between two chunks — guards against a total stall without
        cutting an active generation mid-stream.
        """
        response = requests.post(
            f"{self.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=build_openai_request(self.provider, self.model, messages, temperature),
            timeout=(10, self.timeout),
            stream=True,
        )
        response.raise_for_status()
        chunks = []
        for line in response.iter_lines():
            # Skip blank lines and non-SSE lines (e.g. HTTP headers in chunked encoding).
            if not line or not line.startswith(b"data: "):
                continue
            data = line[6:]
            # OpenAI-compatible streaming API terminates with a [DONE] sentinel.
            if data == b"[DONE]":
                break
            choices = json.loads(data).get("choices") or []
            delta = choices[0].get("delta", {}).get("content", "") if choices else ""
            if delta:
                chunks.append(delta)
        return "".join(chunks)

    def _anthropic_chat(self, messages: list, temperature: float) -> str:
        kwargs = build_anthropic_request(self.model, messages, temperature, self.effort)
        with self._anthropic.messages.stream(**kwargs) as stream:
            text = "".join(stream.text_stream)
            final = stream.get_final_message()
        if final.stop_reason == "refusal":
            category = final.stop_details.category if final.stop_details else None
            raise RuntimeError(f"Anthropic a refusé la requête (catégorie : {category})")
        return text
