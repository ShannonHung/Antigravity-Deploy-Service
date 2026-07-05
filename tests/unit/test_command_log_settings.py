from app.core.config import get_settings


def test_command_log_settings_present():
    get_settings.cache_clear()
    s = get_settings()
    assert s.COMMAND_LOG_DIR == "/var/log/deploy-service"
    assert s.COMMAND_LOG_SOFT_CAP_BYTES == 5 * 1024 * 1024
    assert s.COMMAND_LOG_HARD_CAP_BYTES == 10 * 1024 * 1024
    assert s.COMMAND_LOG_FAILURE_TAIL_LINES == 50


def test_command_log_dir_default_is_deploy_service():
    get_settings.cache_clear()
    assert get_settings().COMMAND_LOG_DIR == "/var/log/deploy-service"
