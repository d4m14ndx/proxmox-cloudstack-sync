from pydantic_settings import BaseSettings
from pydantic import Field, field_validator, model_validator
from typing import Literal, Optional
import json
import os
import uuid


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


class AdoptionPolicy(BaseSettings):
    """Fail-closed policy for read-only adoption planning.

    Project ownership is intentionally not configurable: adoption plans always
    target the CloudStack ROOT-domain admin account with no project.
    """

    enabled: bool = False
    account: Literal["admin"] = "admin"
    domain_id: str = ""
    customized_service_offering_id: str = ""
    template_id: str = ""

    @field_validator("domain_id", "customized_service_offering_id", "template_id")
    @classmethod
    def validate_uuid_if_present(cls, value: str) -> str:
        if not value:
            return value
        if value != value.strip():
            raise ValueError("adoption policy UUIDs must already be normalized")
        try:
            return str(uuid.UUID(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("adoption policy value must be a UUID") from exc

    @model_validator(mode="after")
    def validate_enabled_policy(self):
        if self.enabled and (
            not self.domain_id or not self.customized_service_offering_id
        ):
            raise ValueError(
                "enabled adoption policy requires domain and customized offering IDs"
            )
        return self


class Settings(BaseSettings):
    database_url: str = "sqlite:///./sync.db"
    sync_interval_seconds: int = 300
    auto_reconcile: bool = False
    nic_sync_enabled: bool = True
    auto_reconcile_nics: bool = False
    api_auth_token: str = ""
    adoption_registry_enabled: bool = False
    adoption_registry_internal_token: str = ""
    adoption_executor_enabled: bool = False
    adoption_executor_interval_seconds: int = Field(default=10, ge=5, le=300)
    adoption_executor_lease_seconds: int = Field(default=60, ge=30, le=600)
    cloudstack: CloudStackConfig = CloudStackConfig()
    cloudstack_db: CloudStackDBConfig = CloudStackDBConfig()
    adoption_policy: AdoptionPolicy = AdoptionPolicy()
    proxmox_clusters: list[ProxmoxCluster] = []

    @field_validator("api_auth_token", "adoption_registry_internal_token")
    @classmethod
    def validate_api_auth_token(cls, value: str) -> str:
        if value and len(value) < 32:
            raise ValueError("configured API tokens must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_adoption_registry(self):
        if self.adoption_registry_enabled and not self.adoption_registry_internal_token:
            raise ValueError(
                "enabled adoption registry requires adoption_registry_internal_token"
            )
        if self.adoption_executor_enabled:
            if not self.adoption_registry_enabled:
                raise ValueError("enabled adoption executor requires adoption registry")
            if not self.adoption_policy.enabled:
                raise ValueError("enabled adoption executor requires adoption policy")
            if not self.adoption_policy.template_id:
                raise ValueError("enabled adoption executor requires adoption template ID")
        return self

    model_config = {"env_prefix": "SYNC_", "env_file": ".env"}


def load_settings() -> Settings:
    config_path = os.environ.get("SYNC_CONFIG", "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            data = json.load(f)
        if auth_token := os.environ.get("SYNC_API_AUTH_TOKEN"):
            data["api_auth_token"] = auth_token
        if registry_token := os.environ.get(
            "SYNC_ADOPTION_REGISTRY_INTERNAL_TOKEN"
        ):
            data["adoption_registry_internal_token"] = registry_token
        if database_url := os.environ.get("SYNC_DATABASE_URL"):
            data["database_url"] = database_url
        if interval := os.environ.get("SYNC_SYNC_INTERVAL_SECONDS"):
            data["sync_interval_seconds"] = int(interval)
        if executor_enabled := os.environ.get("SYNC_ADOPTION_EXECUTOR_ENABLED"):
            data["adoption_executor_enabled"] = executor_enabled.lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if executor_interval := os.environ.get(
            "SYNC_ADOPTION_EXECUTOR_INTERVAL_SECONDS"
        ):
            data["adoption_executor_interval_seconds"] = int(executor_interval)
        if executor_lease := os.environ.get("SYNC_ADOPTION_EXECUTOR_LEASE_SECONDS"):
            data["adoption_executor_lease_seconds"] = int(executor_lease)
        settings = Settings(**data)
    else:
        settings = Settings()

    return settings
