"""Centralized runtime settings loaded from environment variables.

Settings are read dynamically via helper functions / get_settings() so
test monkeypatches on os.environ take effect immediately without stale
module-level caches.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Runtime
    system_timezone: str
    app_base_url: str
    cors_origins: tuple[str, ...]

    # Database
    database_url: str

    # Auth & JWT
    jwt_secret: str
    access_token_ttl_seconds: int
    refresh_token_ttl_days: int
    auth_allow_legacy_role_header: bool
    email_code_ttl_seconds: int

    # OAuth providers
    oauth_redirect_base_url: str
    oauth_github_client_id: str
    oauth_github_client_secret: str
    oauth_google_client_id: str
    oauth_google_client_secret: str
    oauth_linuxdo_client_id: str
    oauth_linuxdo_client_secret: str

    # Email SMTP
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str

    # Object Storage (S3-compatible + local signed fallback)
    storage_backend: str  # auto | s3 | local
    s3_endpoint_url: str
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str
    s3_public_base_url: str
    local_storage_dir: Path
    storage_signing_secret: str
    user_storage_quota_bytes: int
    avatar_max_bytes: int
    cover_max_bytes: int
    cover_video_max_seconds: float

    # Pricing placeholders
    llm_price_input: float
    llm_price_output: float

    # P1: Realtime collaboration, Billing & Multimodal Model Gateway
    object_lease_ttl_seconds: int
    presence_ttl_seconds: int
    default_signup_bonus_credits: float
    voice_first_use_credits: float
    model_gateway_base_url: str
    model_gateway_api_key: str

    # P2: Context compression, TaskRun leases & Sandbox
    context_compress_threshold_tokens: int
    task_run_lease_ttl_seconds: int
    sandbox_default_cap_credits: float
    sandbox_default_idle_minutes: int

    # P3: Skills upload, MCP OAuth DCR & BYOK encryption
    skill_zip_max_bytes: int
    mcp_grant_default_ttl_days: int
    byok_encryption_key: str

    # P4: 4C8G Production Hardening (Redis, ARQ, DB pool, FFmpeg, Sandbox & Rate limits)
    redis_url: str
    async_worker_enabled: bool
    db_pool_size: int
    db_max_overflow: int
    sandbox_max_concurrent: int
    sandbox_docker_memory: str
    sandbox_docker_cpus: str
    ffmpeg_threads: int
    ffmpeg_max_concurrent: int
    rate_limit_login_per_min: int
    rate_limit_email_code_per_min: int
    orphan_upload_max_age_hours: int

    @property
    def effective_storage_backend(self) -> str:
        mode = self.storage_backend.strip().lower()
        if mode == "s3":
            return "s3"
        if mode == "local":
            return "local"
        return "s3" if (self.s3_bucket and self.s3_access_key_id and self.s3_secret_access_key) else "local"


def get_settings() -> Settings:
    raw_cors = os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://8.220.194.80:5173",
    )
    cors_origins = tuple(o.strip() for o in raw_cors.split(",") if o.strip())
    jwt_secret = os.environ.get("JWT_SECRET", "dev-insecure-canvasphere-jwt-secret-change-me")
    default_storage_dir = Path(os.environ.get("LOCAL_STORAGE_DIR", str(Path(__file__).resolve().parents[2] / "storage_data")))
    app_base_url = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    return Settings(
        system_timezone=os.environ.get("SYSTEM_TIMEZONE", "Asia/Shanghai"),
        app_base_url=app_base_url,
        cors_origins=cors_origins,
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        jwt_secret=jwt_secret,
        access_token_ttl_seconds=_int_env("ACCESS_TOKEN_TTL_SECONDS", 7200),
        refresh_token_ttl_days=_int_env("REFRESH_TOKEN_TTL_DAYS", 30),
        auth_allow_legacy_role_header=_bool_env("AUTH_ALLOW_LEGACY_ROLE_HEADER", False),
        email_code_ttl_seconds=_int_env("EMAIL_CODE_TTL_SECONDS", 600),
        oauth_redirect_base_url=os.environ.get("OAUTH_REDIRECT_BASE_URL", app_base_url).rstrip("/"),
        oauth_github_client_id=os.environ.get("OAUTH_GITHUB_CLIENT_ID", "").strip(),
        oauth_github_client_secret=os.environ.get("OAUTH_GITHUB_CLIENT_SECRET", "").strip(),
        oauth_google_client_id=os.environ.get("OAUTH_GOOGLE_CLIENT_ID", "").strip(),
        oauth_google_client_secret=os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET", "").strip(),
        oauth_linuxdo_client_id=os.environ.get("OAUTH_LINUXDO_CLIENT_ID", "").strip(),
        oauth_linuxdo_client_secret=os.environ.get("OAUTH_LINUXDO_CLIENT_SECRET", "").strip(),
        smtp_host=os.environ.get("SMTP_HOST", "").strip(),
        smtp_port=_int_env("SMTP_PORT", 587),
        smtp_user=os.environ.get("SMTP_USER", "").strip(),
        smtp_password=os.environ.get("SMTP_PASSWORD", "").strip(),
        smtp_from=os.environ.get("SMTP_FROM", "").strip(),
        storage_backend=os.environ.get("STORAGE_BACKEND", "auto").strip().lower() or "auto",
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL", "").strip(),
        s3_bucket=os.environ.get("S3_BUCKET", "").strip(),
        s3_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "").strip(),
        s3_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "").strip(),
        s3_region=os.environ.get("S3_REGION", "auto").strip(),
        s3_public_base_url=os.environ.get("S3_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        local_storage_dir=default_storage_dir,
        storage_signing_secret=os.environ.get("STORAGE_SIGNING_SECRET", jwt_secret),
        user_storage_quota_bytes=_int_env("USER_STORAGE_QUOTA_BYTES", 1024 * 1024 * 1024),
        avatar_max_bytes=_int_env("AVATAR_MAX_BYTES", 2 * 1024 * 1024),
        cover_max_bytes=_int_env("COVER_MAX_BYTES", 50 * 1024 * 1024),
        cover_video_max_seconds=_float_env("COVER_VIDEO_MAX_SECONDS", 30.0),
        llm_price_input=_float_env("LLM_PRICE_INPUT", 30.0),
        llm_price_output=_float_env("LLM_PRICE_OUTPUT", 150.0),
        object_lease_ttl_seconds=_int_env("OBJECT_LEASE_TTL_SECONDS", 60),
        presence_ttl_seconds=_int_env("PRESENCE_TTL_SECONDS", 30),
        default_signup_bonus_credits=_float_env("DEFAULT_SIGNUP_BONUS_CREDITS", 300.0),
        voice_first_use_credits=_float_env("VOICE_FIRST_USE_CREDITS", 1000.0),
        model_gateway_base_url=os.environ.get("MODEL_GATEWAY_BASE_URL", "").strip().rstrip("/"),
        model_gateway_api_key=os.environ.get("MODEL_GATEWAY_API_KEY", "").strip(),
        context_compress_threshold_tokens=_int_env("CONTEXT_COMPRESS_THRESHOLD_TOKENS", 4000),
        task_run_lease_ttl_seconds=_int_env("TASK_RUN_LEASE_TTL_SECONDS", 120),
        sandbox_default_cap_credits=_float_env("SANDBOX_DEFAULT_CAP_CREDITS", 500.0),
        sandbox_default_idle_minutes=_int_env("SANDBOX_DEFAULT_IDLE_MINUTES", 2),
        skill_zip_max_bytes=_int_env("SKILL_ZIP_MAX_BYTES", 10 * 1024 * 1024),
        mcp_grant_default_ttl_days=_int_env("MCP_GRANT_DEFAULT_TTL_DAYS", 30),
        byok_encryption_key=os.environ.get("BYOK_ENCRYPTION_KEY", "").strip(),
        redis_url=os.environ.get("REDIS_URL", "").strip(),
        async_worker_enabled=_bool_env("ASYNC_WORKER_ENABLED", False),
        db_pool_size=_int_env("DB_POOL_SIZE", 5),
        db_max_overflow=_int_env("DB_MAX_OVERFLOW", 10),
        sandbox_max_concurrent=_int_env("SANDBOX_MAX_CONCURRENT", 2),
        sandbox_docker_memory=os.environ.get("SANDBOX_DOCKER_MEMORY", "512m").strip() or "512m",
        sandbox_docker_cpus=os.environ.get("SANDBOX_DOCKER_CPUS", "0.5").strip() or "0.5",
        ffmpeg_threads=_int_env("FFMPEG_THREADS", 2),
        ffmpeg_max_concurrent=_int_env("FFMPEG_MAX_CONCURRENT", 1),
        rate_limit_login_per_min=_int_env("RATE_LIMIT_LOGIN_PER_MIN", 20),
        rate_limit_email_code_per_min=_int_env("RATE_LIMIT_EMAIL_CODE_PER_MIN", 5),
        orphan_upload_max_age_hours=_int_env("ORPHAN_UPLOAD_MAX_AGE_HOURS", 24),
    )
