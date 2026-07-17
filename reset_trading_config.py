"""Apply the daily 15:30 IST capital-profile reset."""

from trading_config import get_config


if __name__ == "__main__":
    config = get_config()
    print(
        f"Active profile: {config['profile']['label']} "
        f"server_time={config['serverTime']}"
    )
