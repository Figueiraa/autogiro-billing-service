from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    SERVICE_NAME: str = "billing-service"
    SERVICE_VERSION: str = "0.1.0"

    # Segredo HS256 compartilhado com a Lambda `autogiro-auth` (emissor) e com o
    # Kong (validador). O serviço revalida a assinatura em vez de confiar no
    # gateway: chamadas internas e a execução local não passam pelo Kong (ADR-004).
    # Obrigatório e sem valor padrão: o serviço não sobe sem ele, e nenhum segredo
    # fica escrito no código.
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "autogiro-auth"

    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "*"  # lista separada por vírgula; use origens explícitas em produção

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()
