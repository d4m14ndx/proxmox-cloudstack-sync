from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional
import json
import os


class ProxmoxCluster(BaseSettings):
    name: str
    host: str = ""
    hosts: list[str] = []
    user: str = "root@pam"
    token_name: str = ""
    token_value: str = ""
    password: Optional[str] = None
    verify_ssl: bool = False

    @property
    def all_hosts(self) -> list[str]:
        """Return hosts list, falling back to singular host for backwards compat."""
        if self.hosts:
            return list(self.hosts)
        if self.host:
            return [self.host]
        return []


class CloudStackConfig(BaseSettings):
    url: str = "http://localhost:8080/client/api"
    api_key: str = ""
    secret_key: str = ""


class Settings(BaseSettings):
    database_url: str = "sqlite:///./sync.db"
    sync_interval_seconds: int = 300
    cloudstack: CloudStackConfig = CloudStackConfig()
    proxmox_clusters: list[ProxmoxCluster] = []

    model_config = {"env_prefix": "SYNC_", "env_file": ".env"}


def load_settings() -> Settings:
    config_path = os.environ.get("SYNC_CONFIG", "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            data = json.load(f)
        settings = Settings(**data)
    else:
        settings = Settings()

    # Env vars override config file for top-level scalars
    if db := os.environ.get("SYNC_DATABASE_URL"):
        settings.database_url = db
    if interval := os.environ.get("SYNC_SYNC_INTERVAL_SECONDS"):
        settings.sync_interval_seconds = int(interval)

    return settings
