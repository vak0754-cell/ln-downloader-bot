from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(..., alias="BOT_TOKEN")
    allowed_user_ids_raw: str = Field("", alias="ALLOWED_USER_IDS")

    data_dir: Path = Field(Path("./data"), alias="DATA_DIR")
    concurrency: int = Field(3, alias="CONCURRENCY")
    rate_limit_delay: float = Field(0.5, alias="RATE_LIMIT_DELAY")
    request_timeout: int = Field(25, alias="REQUEST_TIMEOUT")
    max_file_mb: float = Field(48.0, alias="MAX_FILE_MB")
    user_agent: str = Field("Mozilla/5.0 (compatible; LNDownloaderBot/1.0)", alias="USER_AGENT")

    drive_root_folder_id: str = Field("1DWI4MenLELKIgy1B61gY4swzCycVSxx6", alias="DRIVE_ROOT_FOLDER_ID")
    google_service_account_file: Path = Field(
        Path("./credentials/service_account.json"), alias="GOOGLE_SERVICE_ACCOUNT_FILE"
    )

    @property
    def allowed_user_ids(self) -> set[int]:
        return {int(x.strip()) for x in self.allowed_user_ids_raw.split(",") if x.strip().isdigit()}

    def ensure_dirs(self) -> None:
        for sub in ("cache", "downloads", "books"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)
