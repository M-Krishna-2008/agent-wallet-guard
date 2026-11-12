"""
Application configuration for Agent Wallet Guard.
Loads settings from environment variables or .env file using pydantic-settings.
"""

from __future__ import annotations

import re
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PayPal Sandbox Configuration
    PAYPAL_CLIENT_ID: str = Field(default="your_paypal_client_id_here")
    PAYPAL_CLIENT_SECRET: str = Field(default="your_paypal_client_secret_here")
    PAYPAL_ENV: str = Field(default="sandbox")
    PAYPAL_MOCK: bool = Field(default=True)
    PAYPAL_MODE: str = Field(default="payouts")  # "payouts" or "orders"
    VENDOR_SANDBOX_EMAIL: str = Field(default="sb-vendor@business.example.com")

    # Agent API Key Authentication
    AGENT_API_KEY: str = Field(default="guard-secret-agent-key-2026")

    # Database Configuration
    DATABASE_URL: str = Field(default="sqlite:///guard.db")

    # LLM Settings
    LLM_PROVIDER: str = Field(default="gemini")
    LLM_API_KEY: str = Field(default="")
    LLM_MODEL: str = Field(default="gemini-3.1-flash-lite-preview")

    # Demo & Runtime Settings
    DEMO_MODE: bool = Field(default=True)
    AGENT_MODE: str = Field(default="scripted")  # "scripted" or "live"
    AGENT_TOOL: str = Field(default="rest")  # "rest" or "mcp"
    PAYPAL_MCP_URL: str = Field(default="https://mcp.sandbox.paypal.com")
    ALLOW_LIVE: bool = Field(default=False)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Prioritize project .env file over ambient machine environment variables
        return (
            init_settings,
            dotenv_settings,
            env_settings,
            file_secret_settings,
        )

    @property
    def paypal_base_url(self) -> str:
        """
        Base URL for PayPal API. Defaults to sandbox.
        Refuses to run against live unless PAYPAL_ENV='live' is set explicitly.
        """
        env = self.PAYPAL_ENV.strip().lower()
        if env == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def normalized_database_url(self) -> str:
        """
        Converts postgres:// or postgresql:// schemes to postgresql+psycopg://
        to ensure driver compatibility with psycopg v3.
        """
        url = self.DATABASE_URL.strip()
        if url.startswith("postgres://"):
            return re.sub(r"^postgres://", "postgresql+psycopg://", url, count=1)
        elif url.startswith("postgresql://") and not url.startswith("postgresql+psycopg://"):
            return re.sub(r"^postgresql://", "postgresql+psycopg://", url, count=1)
        return url


settings = Settings()
