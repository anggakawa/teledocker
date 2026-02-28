"""Tests for the _build_env_vars logic in sessions router.

The _build_env_vars function maps a User's stored API key and provider
config into the correct environment variables for Claude CLI. This is
the core of the per-message env injection pipeline:

  User DB record  -->  _build_env_vars()  -->  env_vars dict  -->  agent-bridge

Because Python 3.15 alpha cannot build pydantic-core (needed to import
the full router module), these tests mirror the function logic — the same
pattern used by test_sse_proxy.py. Any change to _build_env_vars in
production must also be reflected here.

Four provider scenarios are tested:
  1. Anthropic (default) — sets ANTHROPIC_API_KEY
  2. OpenRouter / custom — sets ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL
  3. No API key stored — returns empty dict
  4. Model selection — sets ANTHROPIC_MODEL when model is configured
"""



# ---------------------------------------------------------------------------
# Mirror of _build_env_vars from api_server/routers/sessions.py
# ---------------------------------------------------------------------------


def _build_env_vars(
    api_key: str | None,
    provider_config: dict | None,
) -> dict[str, str]:
    """Build Claude CLI env vars from a decrypted API key and provider config.

    This mirrors the production _build_env_vars, except it takes the already-
    decrypted key and provider_config directly (no User model / settings needed).
    """
    if not api_key:
        return {}

    provider_config = provider_config or {}
    provider = provider_config.get("provider", "anthropic")
    base_url = provider_config.get("base_url")

    model = provider_config.get("model")

    if provider == "anthropic":
        env = {"ANTHROPIC_API_KEY": api_key}
    else:
        # OpenRouter or custom provider — use base URL + auth token.
        env = {
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_API_KEY": "",  # Must be explicitly empty
        }
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url

    if model:
        env["ANTHROPIC_MODEL"] = model

    return env


# ---------------------------------------------------------------------------
# Tests: Anthropic provider (default)
# ---------------------------------------------------------------------------


class TestBuildEnvVarsAnthropic:
    """When provider is 'anthropic', only ANTHROPIC_API_KEY should be set."""

    def test_returns_anthropic_api_key(self):
        """Standard Anthropic user gets a single ANTHROPIC_API_KEY entry."""
        result = _build_env_vars(
            api_key="sk-ant-test-key-123",
            provider_config={"provider": "anthropic"},
        )

        assert result == {"ANTHROPIC_API_KEY": "sk-ant-test-key-123"}

    def test_does_not_include_auth_token_or_base_url(self):
        """Anthropic provider should not set AUTH_TOKEN or BASE_URL."""
        result = _build_env_vars(
            api_key="sk-ant-key",
            provider_config={"provider": "anthropic"},
        )

        assert "ANTHROPIC_AUTH_TOKEN" not in result
        assert "ANTHROPIC_BASE_URL" not in result

    def test_defaults_to_anthropic_when_provider_not_specified(self):
        """When provider_config has no 'provider' key, default to anthropic."""
        result = _build_env_vars(
            api_key="sk-ant-key",
            provider_config={},
        )

        assert result == {"ANTHROPIC_API_KEY": "sk-ant-key"}


# ---------------------------------------------------------------------------
# Tests: OpenRouter / custom provider
# ---------------------------------------------------------------------------


class TestBuildEnvVarsOpenRouter:
    """When provider is not 'anthropic', AUTH_TOKEN and BASE_URL are used."""

    def test_openrouter_sets_auth_token_and_empty_api_key(self):
        """OpenRouter users get AUTH_TOKEN with an explicitly empty API_KEY."""
        result = _build_env_vars(
            api_key="sk-or-token-456",
            provider_config={
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api",
            },
        )

        assert result["ANTHROPIC_AUTH_TOKEN"] == "sk-or-token-456"
        assert result["ANTHROPIC_API_KEY"] == ""
        assert result["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"

    def test_custom_provider_without_base_url_omits_base_url(self):
        """A custom provider with no base_url should not include BASE_URL key."""
        result = _build_env_vars(
            api_key="custom-key",
            provider_config={"provider": "custom", "base_url": None},
        )

        assert "ANTHROPIC_AUTH_TOKEN" in result
        assert "ANTHROPIC_BASE_URL" not in result

    def test_api_key_is_explicitly_empty_string_for_custom_provider(self):
        """Claude CLI requires ANTHROPIC_API_KEY to exist (even empty) with custom base."""
        result = _build_env_vars(
            api_key="my-token",
            provider_config={"provider": "openrouter", "base_url": "https://example.com"},
        )

        assert "ANTHROPIC_API_KEY" in result
        assert result["ANTHROPIC_API_KEY"] == ""


# ---------------------------------------------------------------------------
# Tests: No API key stored
# ---------------------------------------------------------------------------


class TestBuildEnvVarsNoKey:
    """When no API key is stored, an empty dict is returned."""

    def test_returns_empty_dict_when_key_is_none(self):
        """A user without a stored API key gets no env vars."""
        result = _build_env_vars(
            api_key=None,
            provider_config={"provider": "anthropic"},
        )

        assert result == {}

    def test_returns_empty_dict_when_key_is_empty_string(self):
        """An empty-string API key is treated as no key."""
        result = _build_env_vars(
            api_key="",
            provider_config={"provider": "anthropic"},
        )

        assert result == {}

    def test_returns_anthropic_key_when_provider_config_is_none(self):
        """A user with a key but None provider_config defaults to anthropic."""
        result = _build_env_vars(
            api_key="sk-ant-key",
            provider_config=None,
        )

        assert result == {"ANTHROPIC_API_KEY": "sk-ant-key"}


# ---------------------------------------------------------------------------
# Tests: Model selection via ANTHROPIC_MODEL
# ---------------------------------------------------------------------------


class TestBuildEnvVarsModel:
    """When model is set in provider_config, ANTHROPIC_MODEL should be injected."""

    def test_model_set_for_anthropic_provider(self):
        """Anthropic user with a model gets both API_KEY and MODEL."""
        result = _build_env_vars(
            api_key="sk-ant-key",
            provider_config={"provider": "anthropic", "model": "opus"},
        )

        assert result["ANTHROPIC_API_KEY"] == "sk-ant-key"
        assert result["ANTHROPIC_MODEL"] == "opus"

    def test_model_set_for_openrouter_provider(self):
        """OpenRouter user with a model gets AUTH_TOKEN, BASE_URL, and MODEL."""
        result = _build_env_vars(
            api_key="sk-or-key",
            provider_config={
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api",
                "model": "sonnet",
            },
        )

        assert result["ANTHROPIC_AUTH_TOKEN"] == "sk-or-key"
        assert result["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
        assert result["ANTHROPIC_MODEL"] == "sonnet"

    def test_model_none_omits_env_var(self):
        """When model is None, ANTHROPIC_MODEL should not be in the env dict."""
        result = _build_env_vars(
            api_key="sk-ant-key",
            provider_config={"provider": "anthropic", "model": None},
        )

        assert "ANTHROPIC_MODEL" not in result

    def test_model_empty_string_omits_env_var(self):
        """When model is an empty string, ANTHROPIC_MODEL should not be set."""
        result = _build_env_vars(
            api_key="sk-ant-key",
            provider_config={"provider": "anthropic", "model": ""},
        )

        assert "ANTHROPIC_MODEL" not in result

    def test_full_model_id_passed_through(self):
        """Full model IDs like 'claude-opus-4-6' should be passed verbatim."""
        result = _build_env_vars(
            api_key="sk-ant-key",
            provider_config={"provider": "anthropic", "model": "claude-opus-4-6"},
        )

        assert result["ANTHROPIC_MODEL"] == "claude-opus-4-6"
