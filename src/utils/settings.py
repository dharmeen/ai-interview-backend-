from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Setting(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DB_CONNECTION:str

    JWT_SECRET:str
    ACCESS_TOKEN_EXPIRE_MINUTES:int = 15
    REFRESH_TOKEN_EXPIRE_DAYS:int = 7

    # comma-separated, e.g. "http://localhost:3000,https://app.example.com"
    FRONTEND_ORIGINS:str = "http://localhost:3000"
    # Secure cookies need HTTPS: false for local http, true in production
    COOKIE_SECURE:bool = False

    # SecretStr prints as '**********', so the key can't leak into logs or error messages.
    # Optional so the app still starts without it; AI routes then return 503.
    MINIMAX_API_KEY:SecretStr | None = None
    # international accounts; accounts from the China site use https://api.minimaxi.com/v1
    MINIMAX_BASE_URL:str = "https://api.minimax.io/v1"
    # small = topic plan and questions (simple, fast); large = final feedback (needs more judgment)
    LLM_MODEL_SMALL:str = "MiniMax-M2.7-highspeed"
    LLM_MODEL_LARGE:str = "MiniMax-M3"
    # used when the main model is overloaded or rate limited (429/5xx); empty = no fallback
    LLM_MODEL_FALLBACK:str = "MiniMax-M2.7"
    # these models think before answering, so give them time
    LLM_TIMEOUT_SECONDS:float = 120.0

    @property
    def frontend_origins(self) -> list[str]:
        origins = [o.strip().rstrip("/") for o in self.FRONTEND_ORIGINS.split(",") if o.strip()]
        # browsers reject "*" together with credentials, and it would allow any site
        if "*" in origins:
            raise ValueError("FRONTEND_ORIGINS must list explicit origins, not '*'")
        return origins

settings =Setting()
