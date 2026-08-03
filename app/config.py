"""
Configuration loader using pydantic-settings.
Loads from .env, environment variables, with sensible defaults.
"""
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- App -----
    APP_NAME: str = "Platform Optimization Agent"
    APP_ENV: str = "development"
    DEBUG: bool = True
    API_PORT: int = 8000
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:5173","http://localhost:5176"]

    # ----- Secrets (used to encrypt cloud credentials at rest) -----
    SECRET_KEY: str = "change-me-in-production-min-32-chars-of-entropy-xxxx"

    # ----- MySQL -----
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "optagent"
    MYSQL_PASSWORD: str = "changeme"
    MYSQL_DATABASE: str = "platform_optimizer"
    DATABASE_URL: Optional[str] = None

    # ----- AWS -----
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_DEFAULT_REGION: str = "us-east-1"
    AWS_SCAN_REGIONS: str = "us-east-1"
    AWS_ACCOUNT_ID: Optional[str] = None

    # ----- Azure -----
    AZURE_TENANT_ID: Optional[str] = None
    AZURE_CLIENT_ID: Optional[str] = None
    AZURE_CLIENT_SECRET: Optional[str] = None
    AZURE_SUBSCRIPTION_ID: Optional[str] = None

    # ----- Mistral -----
    MISTRAL_API_KEY: Optional[str] = None
    MISTRAL_MODEL_FRONTIER: str = "mistral-large-latest"
    MISTRAL_MODEL_EFFICIENT: str = "mistral-small-latest"
    MISTRAL_EMBED_MODEL: str = "mistral-embed"

    # ----- ChromaDB -----
    CHROMA_PERSIST_DIR: str = "./chroma_data"
    CHROMA_COLLECTION_PLAYBOOKS: str = "optimization_playbooks"
    CHROMA_COLLECTION_EPISODIC: str = "episodic_memory"
    CHROMA_COLLECTION_SEMANTIC: str = "semantic_memory"

    # ----- Redis -----
    REDIS_URL: str = "redis://localhost:6379/0"

    # ----- Agent Behavior -----
    AUTO_EXECUTE_LOW_RISK: bool = True
    APPROVE_WINDOW_MEDIUM_RISK_MINUTES: int = 15
    BLAST_RADIUS_MAX_RESOURCES_PER_HOUR: int = 10
    COST_ANOMALY_THRESHOLD_PCT: float = 25.0
    IDLE_CPU_THRESHOLD_PCT: float = 10.0
    TELEMETRY_LOOKBACK_DAYS: int = 14

    # ----- Auto-scan scheduler -----
    # Set to 0 to disable. Otherwise: minutes between automatic scans.
    SCAN_INTERVAL_MINUTES: int = 60
    SCAN_ON_STARTUP: bool = False   # If true, run one scan when the app boots
    SCAN_PROVIDER: str = "all"      # 'aws' | 'azure' | 'all' for the scheduled scan

    @property
    def database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
        )

    @property
    def aws_regions_list(self) -> List[str]:
        return [r.strip() for r in self.AWS_SCAN_REGIONS.split(",") if r.strip()]

    @property
    def cors_origins_list(self) -> List[str]:
        if isinstance(self.CORS_ORIGINS, list):
            return self.CORS_ORIGINS
        return [o.strip() for o in str(self.CORS_ORIGINS).split(",") if o.strip()]

    # Aliases for backwards-compatible naming
    @property
    def ENVIRONMENT(self) -> str:
        return self.APP_ENV

    @property
    def LOG_LEVEL(self) -> str:
        return "DEBUG" if self.DEBUG else "INFO"

    @property
    def MISTRAL_FRONTIER_MODEL(self) -> str:
        return self.MISTRAL_MODEL_FRONTIER

    @property
    def MISTRAL_EFFICIENT_MODEL(self) -> str:
        return self.MISTRAL_MODEL_EFFICIENT


settings = Settings()
