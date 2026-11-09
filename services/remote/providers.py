"""The hosted-model providers IMURUN knows, and how to reach each one.

Every provider speaks the OpenAI-compatible chat API, so one client
(``model/remote_client.py``) serves them all. What differs is listed here:
the fixed base URL and the hosts its key may be sent to (G5), where to get a
key, what a key looks like, which models to suggest, and small API
differences (OpenRouter's optional attribution headers, OpenAI's
``max_completion_tokens``, providers that do not understand
``stream_options``, NVIDIA's public model list).

Base URLs checked against each provider's documentation on 2026-11-09:
NVIDIA (docs.api.nvidia.com: integrate.api.nvidia.com + /v1/chat/completions),
OpenRouter (openrouter.ai/docs: https://openrouter.ai/api/v1, GET /models needs
a bearer key and pages with links.next, HTTP-Referer / X-OpenRouter-Title
headers), DeepSeek (api-docs.deepseek.com: https://api.deepseek.com without
/v1, GET /models, models deepseek-flash and deepseek-v4-pro), Groq
(console.groq.com: https://api.groq.com/openai/v1, GET /models), Together
(docs.together.ai: https://api.together.ai/v1, GET /v1/models), Mistral
(docs.mistral.ai: https://api.mistral.ai/v1, GET /v1/models with
capabilities.completion_chat and max_context_length). OpenAI's reference
refused automated reads; https://api.openai.com/v1 is the long-standing
documented base. Recommended model ids other than DeepSeek's and Groq's
were not checked against a live catalogue (that needs a key); the catalogue
a key actually offers always wins, and a stale id fails with a clear
"no longer offers" error.

The custom provider has no fixed URL: the user enters one, it must be https
unless the host is loopback, and it is kept in the preference file (it is
not a secret). Its key may only go to the host the user entered.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

try:  # run as ``imurun.services...``
    from ...model.nvidia_client import NVIDIA_BASE_URL, NVIDIA_HOST, PROBE_MODEL
    from ...model.nvidia_client import RECOMMENDED as NVIDIA_RECOMMENDED
    from ...model.remote_client import is_loopback
except ImportError:  # run from the imurun directory
    from model.nvidia_client import NVIDIA_BASE_URL, NVIDIA_HOST, PROBE_MODEL
    from model.nvidia_client import RECOMMENDED as NVIDIA_RECOMMENDED
    from model.remote_client import is_loopback

APP_URL = "https://github.com/shubham001312/imurun"
CUSTOM = "custom"


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    base_url: str                     # "" for custom
    hosts: tuple[str, ...]            # where its key may be sent (G5); custom: filled per URL
    key_url: str                      # where to get a key
    key_format: str                   # plain-words hint shown by the key field
    key_prefixes: tuple[str, ...] = ()  # enforced only when ``strict_prefix``
    strict_prefix: bool = False
    recommended: tuple[str, ...] = ()
    default_count: int = 2            # recommended models switched on at connect
    verify: str = "models"            # "models" (authenticated GET /models) or "chat" (one token)
    probe_model: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    token_param: str = "max_tokens"
    stream_usage: bool = True
    no_sampling_prefixes: tuple[str, ...] = ()
    env_key: str = ""
    description: str = ""
    custom: bool = False
    tags: tuple[str, ...] = field(default=())

    @property
    def secret_name(self) -> str:
        return f"model.{self.id}"

    def env_base_url(self) -> str:
        """Tests (and the e2e run) point a provider at a loopback fake with
        ``IMURUN_<ID>_BASE_URL``; the client only accepts it when it is the
        provider's own host or loopback."""
        return os.getenv(f"IMURUN_{self.id.upper()}_BASE_URL", "").strip()

    def view(self) -> dict:
        return {"id": self.id, "label": self.label, "base_url": self.base_url, "hosts": list(self.hosts),
                "key_url": self.key_url, "key_format": self.key_format, "recommended": list(self.recommended),
                "custom": self.custom, "description": self.description}


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider(
        id="nvidia", label="NVIDIA", base_url=NVIDIA_BASE_URL, hosts=(NVIDIA_HOST,),
        key_url="https://build.nvidia.com/", key_format="Starts with nvapi-",
        key_prefixes=("nvapi-",), strict_prefix=True,
        recommended=NVIDIA_RECOMMENDED, default_count=3, verify="chat", probe_model=PROBE_MODEL,
        env_key="NVIDIA_API_KEY",
        description="Free developer keys for many open models on build.nvidia.com."),
    Provider(
        id="openrouter", label="OpenRouter", base_url="https://openrouter.ai/api/v1", hosts=("openrouter.ai",),
        key_url="https://openrouter.ai/settings/keys", key_format="Starts with sk-or-",
        key_prefixes=("sk-or-",),
        recommended=("deepseek/deepseek-chat", "meta-llama/llama-3.3-70b-instruct", "openai/gpt-4o-mini",
                     "google/gemini-2.5-flash", "anthropic/claude-sonnet-4"),
        headers=(("HTTP-Referer", APP_URL), ("X-OpenRouter-Title", "IMURUN"), ("X-Title", "IMURUN")),
        env_key="IMURUN_OPENROUTER_API_KEY",
        description="One key for hundreds of models from many labs, some free."),
    Provider(
        id="deepseek", label="DeepSeek", base_url="https://api.deepseek.com", hosts=("api.deepseek.com",),
        key_url="https://platform.deepseek.com/api_keys", key_format="Starts with sk-",
        key_prefixes=("sk-",),
        recommended=("deepseek-flash", "deepseek-v4-pro"),
        env_key="IMURUN_DEEPSEEK_API_KEY",
        description="DeepSeek's own chat and reasoning models."),
    Provider(
        id="openai", label="OpenAI", base_url="https://api.openai.com/v1", hosts=("api.openai.com",),
        key_url="https://platform.openai.com/api-keys", key_format="Starts with sk-",
        key_prefixes=("sk-",),
        recommended=("gpt-5-mini", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini"),
        token_param="max_completion_tokens", no_sampling_prefixes=("o1", "o3", "o4", "gpt-5"),
        env_key="IMURUN_OPENAI_API_KEY",
        description="GPT models from OpenAI."),
    Provider(
        id="groq", label="Groq", base_url="https://api.groq.com/openai/v1", hosts=("api.groq.com",),
        key_url="https://console.groq.com/keys", key_format="Starts with gsk_",
        key_prefixes=("gsk_",),
        recommended=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b",
                     "openai/gpt-oss-20b"),
        env_key="IMURUN_GROQ_API_KEY",
        description="Very fast replies from open models."),
    Provider(
        id="together", label="Together", base_url="https://api.together.ai/v1", hosts=("api.together.ai",),
        key_url="https://api.together.ai/settings/api-keys", key_format="A long key from your Together account",
        recommended=("meta-llama/Llama-3.3-70B-Instruct-Turbo", "deepseek-ai/DeepSeek-V3",
                     "Qwen/Qwen2.5-72B-Instruct-Turbo"),
        env_key="IMURUN_TOGETHER_API_KEY",
        description="Open models hosted by Together AI."),
    Provider(
        id="mistral", label="Mistral", base_url="https://api.mistral.ai/v1", hosts=("api.mistral.ai",),
        key_url="https://console.mistral.ai/api-keys", key_format="A 32-character key from La Plateforme",
        recommended=("mistral-small-latest", "mistral-large-latest", "codestral-latest"),
        stream_usage=False,  # stream_options is not in Mistral's documented request body
        env_key="IMURUN_MISTRAL_API_KEY",
        description="Mistral's models from La Plateforme."),
    Provider(
        id=CUSTOM, label="Custom (OpenAI-compatible)", base_url="", hosts=(),
        key_url="", key_format="Whatever key your server expects; leave empty if it needs none",
        verify="models", stream_usage=False, custom=True, env_key="",
        description="Any server with an OpenAI-compatible API: vLLM, LM Studio, Ollama, LiteLLM, a company gateway."),
)}

ORDER = tuple(PROVIDERS)


def get(provider_id: str) -> Provider | None:
    return PROVIDERS.get((provider_id or "").strip().lower())


def split_id(model_id: str) -> tuple[Provider, str] | None:
    """``"openrouter:deepseek/deepseek-r1:free"`` -> (OpenRouter, "deepseek/deepseek-r1:free").

    Only the first colon separates: model ids may contain colons."""
    prefix, sep, rest = (model_id or "").partition(":")
    provider = get(prefix) if sep else None
    if provider is None or not rest.strip():
        return None
    return provider, rest.strip()


def key_mismatch(provider: Provider, key: str) -> str:
    """A plain-words reason when ``key`` is clearly another provider's, else ""."""
    if provider.custom:
        return ""  # a gateway may forward any provider's key
    if provider.strict_prefix and provider.key_prefixes and not key.startswith(provider.key_prefixes):
        return f"{provider.label} keys start with {provider.key_prefixes[0]}. Copy it again from {provider.key_url}."
    for other in PROVIDERS.values():
        if other.id == provider.id:
            continue
        # "sk-" is shared by several providers, so only distinctive prefixes count.
        for prefix in other.key_prefixes:
            if prefix != "sk-" and key.startswith(prefix) and prefix not in provider.key_prefixes:
                return f"That looks like a key for {other.label}, not for {provider.label}."
    return ""


class BadBaseUrl(ValueError):
    """A custom base URL that cannot be used. The message is shown as is."""


def clean_custom_url(raw: object) -> str:
    """Validate a user-entered base URL: https, or http to loopback only."""
    if not isinstance(raw, str) or not raw.strip():
        raise BadBaseUrl("Enter the server's base URL, for example https://llm.example.com/v1.")
    url = raw.strip().rstrip("/")
    if len(url) > 500:
        raise BadBaseUrl("That address is too long.")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise BadBaseUrl("The base URL must start with https:// and name a host.")
    if parts.username or parts.password:
        raise BadBaseUrl("Leave the user name and password out of the address; paste the key in the key field.")
    if parts.query or parts.fragment:
        raise BadBaseUrl("The base URL cannot have a ? query or # part.")
    if parts.scheme == "http" and not is_loopback(host):
        raise BadBaseUrl("Use https for servers on other machines. Plain http is only allowed for this "
                         "computer (localhost or 127.0.0.1), so the key is never sent unencrypted.")
    try:
        parts.port
    except ValueError:
        raise BadBaseUrl("The port in that address is not a number.") from None
    return url


def custom_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def custom_origin(url: str) -> str:
    """``scheme://host:port`` — what a custom key is bound to. The port counts:
    localhost:8000 and localhost:9999 can be different programs."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return ""
    return f"{parts.scheme}://{host}:{port}"


def key_problem(key: str) -> str:
    """Why ``key`` cannot go in an Authorization header, else "".

    A line break would make http.client raise with the whole header value in
    its message, and whitespace inside a key is always a paste mistake."""
    if any(ch.isspace() for ch in key):
        return "it contains a space or a line break; copy it again as one line"
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        return "it contains characters an API key cannot have"
    return ""
