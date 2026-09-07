import pytest

from tg_collector.config import load_config


def test_load_config_resolves_paths_and_environment_secrets(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
timezone: Asia/Shanghai
database_path: data/telegram.sqlite3
summary_dir: data/summaries
retention_days: 6
summary_times: ["08:00", "12:00", "22:00"]
cleanup:
  weekday: 6
  time: "03:00"
telegram:
  session_path: data/telegram
  allowlist: ["@hezu1"]
  shop_url_channels: ["@hezu1"]
ai:
  max_input_chars: 12000
  timeout_seconds: 90
  max_retries: 3
  prompt_version: v1
""".strip(),
        encoding="utf-8",
    )
    env = {
        "TG_COLLECTOR_TELEGRAM_API_ID": "12345",
        "TG_COLLECTOR_TELEGRAM_API_HASH": "hash-secret",
        "TG_COLLECTOR_TELEGRAM_PHONE": "+8613800000000",
        "TG_COLLECTOR_OPENAI_BASE_URL": "https://ai.example/v1",
        "TG_COLLECTOR_OPENAI_API_KEY": "ai-secret",
        "TG_COLLECTOR_OPENAI_MODEL": "model-name",
    }

    config = load_config(config_path, environ=env)

    assert config.database_path == tmp_path / "data/telegram.sqlite3"
    assert config.summary_dir == tmp_path / "data/summaries"
    assert config.telegram.session_path == tmp_path / "data/telegram"
    assert config.telegram.allowlist == ("@hezu1",)
    assert config.telegram.shop_url_channels == ("@hezu1",)
    assert config.telegram.api_id == 12345
    assert config.ai.base_url == "https://ai.example/v1"
    assert config.ai.model == "model-name"
    assert "ai-secret" not in repr(config)
    assert config.summary_times == ("08:00", "12:00", "22:00")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("timeout_seconds", 0, "timeout_seconds must be positive"),
        ("max_retries", -1, "max_retries must not be negative"),
    ],
)
def test_ai_retry_and_timeout_values_are_validated(tmp_path, field, value, message):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
timezone: Asia/Shanghai
summary_times: ["08:00"]
telegram:
  allowlist: ["@hezu1"]
ai:
  {field}: {value}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_config(config_path, environ={})


@pytest.mark.parametrize(
    ("config_text", "message"),
    [
        ('summary_times: ["08:00:59"]\ntelegram: {allowlist: ["@hezu1"]}', "clock value must use HH:MM"),
        ('summary_times: ["08:00+08:00"]\ntelegram: {allowlist: ["@hezu1"]}', "clock value must use HH:MM"),
        ('summary_times: ["08:00"]\nretention_days: 6.5\ntelegram: {allowlist: ["@hezu1"]}', "retention_days must be an integer"),
        ('summary_times: ["08:00"]\ncleanup: {weekday: 1.5, time: "03:00"}\ntelegram: {allowlist: ["@hezu1"]}', "cleanup.weekday must be an integer"),
        ('summary_times: ["08:00"]\ntelegram: {allowlist: "@hezu1"}', "telegram.allowlist must be a list"),
        ('summary_times: ["08:00"]\ntelegram: {allowlist: ["@hezu1"]}\nai: {max_retries: 2.5}', "ai.max_retries must be an integer"),
    ],
)
def test_invalid_configuration_types_are_rejected_without_coercion(
    tmp_path, config_text, message
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match=message):
        load_config(config_path, environ={})


def test_shop_url_channel_accepts_equivalent_allowlist_reference_forms(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
summary_times: ["08:00"]
telegram:
  allowlist: ["@SHOPCHANNEL"]
  shop_url_channels: ["https://t.me/shopchannel"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path, environ={})

    assert config.telegram.shop_url_channels == ("https://t.me/shopchannel",)


def test_shop_url_channel_defers_numeric_and_username_equivalence_to_telegram_resolution(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
summary_times: ["08:00"]
telegram:
  allowlist: ["-1000000000123"]
  shop_url_channels: ["@shopchannel"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path, environ={})

    assert config.telegram.shop_url_channels == ("@shopchannel",)


@pytest.mark.parametrize(
    "config_text",
    [
        'summary_times: ["08:00"]\nretantion_days: 6\ntelegram: {allowlist: ["@hezu1"]}',
        'summary_times: ["08:00"]\ntelegram: {allowlist: ["@hezu1"], shop_url_chanels: ["@hezu1"]}',
        'summary_times: ["08:00"]\ntelegram: {allowlist: ["@hezu1"]}\nai: {timeout_second: 90}',
    ],
)
def test_unknown_configuration_keys_are_rejected(tmp_path, config_text):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ValueError, match="unknown configuration key"):
        load_config(config_path, environ={})


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_ai_timeout_must_be_finite(tmp_path, value):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
summary_times: ["08:00"]
telegram: {{allowlist: ["@hezu1"]}}
ai: {{timeout_seconds: {value}}}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="finite"):
        load_config(config_path, environ={})


def test_blank_telegram_source_entry_is_rejected(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
summary_times: ["08:00"]
telegram:
  allowlist: ["@hezu1", "  "]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not be blank"):
        load_config(config_path, environ={})
