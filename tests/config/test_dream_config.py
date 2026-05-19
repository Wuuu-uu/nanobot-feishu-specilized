from nanobot.config.schema import AgentDefaults, DreamConfig


def test_dream_config_defaults_to_interval_hours() -> None:
    cfg = DreamConfig()

    assert cfg.interval_h == 2
    assert cfg.cron is None
    assert cfg.cold_archive_older_than_days == 30
    assert cfg.allow_topic_edits is True


def test_dream_config_builds_every_schedule_from_interval() -> None:
    cfg = DreamConfig(interval_h=3)

    schedule = cfg.build_schedule("UTC")

    assert schedule.kind == "every"
    assert schedule.every_ms == 3 * 3_600_000
    assert schedule.expr is None


def test_dream_config_honors_legacy_cron_override() -> None:
    cfg = DreamConfig.model_validate({"cron": "0 */4 * * *"})

    schedule = cfg.build_schedule("UTC")

    assert schedule.kind == "cron"
    assert schedule.expr == "0 */4 * * *"
    assert schedule.tz == "UTC"
    assert cfg.describe_schedule() == "cron 0 */4 * * * (legacy)"


def test_dream_config_dump_uses_interval_h_and_hides_legacy_cron() -> None:
    cfg = DreamConfig.model_validate({"intervalH": 5, "cron": "0 */4 * * *"})

    dumped = cfg.model_dump(by_alias=True)

    assert dumped["intervalH"] == 5
    assert "cron" not in dumped


def test_dream_config_uses_model_override_name_and_accepts_legacy_model() -> None:
    cfg = DreamConfig.model_validate({"model": "openrouter/sonnet"})

    dumped = cfg.model_dump(by_alias=True)

    assert cfg.model_override == "openrouter/sonnet"
    assert dumped["modelOverride"] == "openrouter/sonnet"
    assert "model" not in dumped


def test_dream_config_accepts_cold_archive_and_topic_aliases() -> None:
    cfg = DreamConfig.model_validate({
        "coldArchiveOlderThanDays": 14,
        "allowTopicEdits": False,
    })

    dumped = cfg.model_dump(by_alias=True)

    assert cfg.cold_archive_older_than_days == 14
    assert cfg.allow_topic_edits is False
    assert dumped["coldArchiveOlderThanDays"] == 14
    assert dumped["allowTopicEdits"] is False


def test_agent_defaults_session_archive_buffer_defaults_and_alias() -> None:
    defaults = AgentDefaults.model_validate({"sessionArchiveBufferMessages": 50})

    dumped = defaults.model_dump(by_alias=True)

    assert defaults.session_archive_buffer_messages == 50
    assert dumped["sessionArchiveBufferMessages"] == 50
