from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:////tmp/craftable_replica.db"
    APP_NAME: str = "Craftable Replica"
    DEBUG: bool = True

settings = Settings()
