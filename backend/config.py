from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
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


class CloudStackDBConfig(BaseSettings):
    host: str = "localhost"
    port: int = 3306
    user: str = "cloud"
    password: str = ""
    database: str = "cloud"
    connect_timeout_seconds: int = Field(default=30, ge=1, le=120)
    read_timeout_seconds: int = Field(default=30, ge=1, le=120)
    write_timeout_seconds: int = Field(default=30, ge=1, le=120)
    reconnect_backoff_seconds: int = Field(default=30, ge=5, le=300)


class Settings(BaseSettings):
    database_url: str = "sqlite:///./sync.db"
    sync_interval_seconds: int = 300
    auto_reconcile: bool = False
    nic_sync_enabled: bool = True
    auto_reconcile_nics: bool = False
    api_auth_token: str = ""
    cloudstack: CloudStackConfig = CloudStackConfig()
    cloudstack_db: CloudStackDBConfig = CloudStackDBConfig()
    proxmox_clusters: list[ProxmoxCluster] = []

    @field_validator("api_auth_token")
    @classmethod
    def validate_api_auth_token(cls, value: str) -> str:
        if value and len(value) < 32:
            raise ValueError("api_auth_token must be at least 32 characters")
        return value

    model_config = {"env_prefix": "SYNC_", "env_file": ".env"}


def load_settings() -> Settings:
    config_path = os.environ.get("SYNC_CONFIG", "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            data = json.load(f)
        if auth_token := os.environ.get("SYNC_API_AUTH_TOKEN"):
            data["api_auth_token"] = auth_token
        settings = Settings(**data)
    else:
        settings = Settings()

    # Env vars override config file for top-level scalars
    if db := os.environ.get("SYNC_DATABASE_URL"):
        settings.database_url = db
    if interval := os.environ.get("SYNC_SYNC_INTERVAL_SECONDS"):
        settings.sync_interval_seconds = int(interval)

    return settings
