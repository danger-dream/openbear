from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator


class BrowserConfig(BaseModel):
    # Opt-in: installation/update alone must not take over a user's live browser.
    enabled: bool = False
    _connection_verified: bool = PrivateAttr(default=False)
    main_endpoint: str = Field(default="", alias="mainEndpoint")
    main_restart_url: str = Field(default="", alias="mainRestartUrl")
    main_download_host_path: str = Field(default="", alias="mainDownloadHostPath")
    main_download_browser_path: str = Field(default="", alias="mainDownloadBrowserPath")
    connect_timeout_s: float = Field(default=10, alias="connectTimeoutS", ge=1, le=120)
    action_timeout_s: float = Field(default=20, alias="actionTimeoutS", ge=1, le=300)
    navigation_timeout_s: float = Field(default=45, alias="navigationTimeoutS", ge=1, le=600)
    max_timeout_s: float = Field(default=180, alias="maxTimeoutS", ge=1, le=1800)
    queue_timeout_s: float = Field(default=10, alias="queueTimeoutS", ge=0.1, le=300)
    snapshot_max_chars: int = Field(default=12000, alias="snapshotMaxChars", ge=1000, le=64000)
    max_event_entries: int = Field(default=200, alias="maxEventEntries", ge=10, le=5000)
    max_body_bytes: int = Field(
        default=2 * 1024 * 1024, alias="maxBodyBytes", ge=1024, le=16 * 1024 * 1024
    )
    max_artifact_bytes: int = Field(
        default=32 * 1024 * 1024, alias="maxArtifactBytes", ge=1024, le=512 * 1024 * 1024
    )
    max_pages_per_owner: int = Field(default=12, alias="maxPagesPerOwner", ge=1, le=100)
    auto_reconnect: bool = Field(default=True, alias="autoReconnect")
    recovery_cooldown_s: float = Field(default=5, alias="recoveryCooldownS", ge=0, le=300)
    allow_evaluate: bool = Field(default=True, alias="allowEvaluate")
    agent_access: bool = Field(default=False, alias="agentAccess")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def migrate_retired_launch_settings(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            # Accept old configuration files without retaining a second launch
            # path. Never infer a service endpoint from an executable/profile.
            for key in (
                "nodeCommand",
                "node_command",
                "defaultMode",
                "default_mode",
                "executablePath",
                "executable_path",
                "headless",
                "noSandbox",
                "no_sandbox",
                "locale",
                "timezone",
                "viewportWidth",
                "viewport_width",
                "viewportHeight",
                "viewport_height",
                "maxIsolatedInstances",
                "max_isolated_instances",
                "idleTimeoutS",
                "idle_timeout_s",
            ):
                value.pop(key, None)
        return value

    @model_validator(mode="after")
    def download_mapping(self):
        from pathlib import Path, PurePosixPath

        # Settings editors may save one field at a time. Validate the pair when
        # connecting, rather than making the second field impossible to enter.
        if self.main_download_host_path and not Path(self.main_download_host_path).is_absolute():
            raise ValueError("mainDownloadHostPath must be absolute")
        if (
            self.main_download_browser_path
            and not PurePosixPath(self.main_download_browser_path).is_absolute()
        ):
            raise ValueError("mainDownloadBrowserPath must be absolute")
        return self

    @field_validator("main_restart_url")
    @classmethod
    def restart_url(cls, value: str) -> str:
        value = cls.endpoint(value)
        if value and urlsplit(value).scheme not in {"http", "https"}:
            raise ValueError("mainRestartUrl must be an http(s) POST endpoint")
        return value

    @field_validator("main_endpoint")
    @classmethod
    def endpoint(cls, value: str) -> str:
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
                raise ValueError("mainEndpoint must be an http(s)/ws(s) CDP endpoint")
            if parsed.username or parsed.password:
                raise ValueError("Do not embed account passwords in mainEndpoint")
        return value
