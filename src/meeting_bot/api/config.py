"""Explicit configuration: importing API never reads the working .env/database."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .state import SecurityPolicy

# D-07 accepted by the owner on 11.10.2026. Limits remain measurable settings.
DEFAULT_POLICY = SecurityPolicy(300, 30, 1800, 86400, 10, 60, 20, 6, 2)


@dataclass(frozen=True)
class ApiConfig:
    bot_token: str = field(repr=False)
    policy_version: str
    policy_url: str
    allowed_origins: tuple[str, ...]
    security: SecurityPolicy = DEFAULT_POLICY
    enabled: bool = True

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("MINI_APP_ENABLED must be boolean")
        if not self.bot_token or not self.policy_version:
            raise ValueError("Incomplete API configuration")
        for origin in self.allowed_origins:
            value = urlsplit(origin)
            if value.scheme != "https" or not value.netloc or value.path != "" or value.query or value.fragment or value.username or value.password:
                raise ValueError("Expected exact HTTPS origin")
        value = urlsplit(self.policy_url)
        if value.scheme != "https" or not value.netloc:
            raise ValueError("Expected HTTPS policy URL")
