from common.config import get_settings, load_yaml_config


def load_calendar_config() -> dict:
    settings = get_settings()
    full_settings = load_yaml_config(settings.settings_config_path)
    return full_settings.get("calendar", {})
