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

    # Banco próprio do serviço: PostgreSQL (Neon em produção).
    DATABASE_URL: str = "sqlite+aiosqlite:///./billing-service.db"

    # Mercado Pago (Checkout Pro). Sem token, aprovar um orçamento responde 502.
    MERCADOPAGO_ACCESS_TOKEN: str | None = None
    MERCADOPAGO_BASE_URL: str = "https://api.mercadopago.com"
    # Campo da preferência usado como link de pagamento: `init_point` (contas de teste
    # e produção) ou `sandbox_init_point` (credenciais de teste antigas, TEST-...).
    MERCADOPAGO_CHECKOUT_LINK: str = "init_point"
    MERCADOPAGO_TIMEOUT_SECONDS: float = 10.0

    # Prazos de negócio.
    APPROVAL_TIMEOUT_MINUTES: int = 1440
    PAYMENT_TIMEOUT_MINUTES: int = 60
    LATE_PAYMENT_WINDOW_HOURS: int = 24
    PAYMENT_POLL_SECONDS: float = 15.0

    # Mensageria (RabbitMQ). Sem URL, a API sobe sem consumir comandos nem publicar
    # eventos: eles ficam no outbox até o broker ser configurado.
    RABBITMQ_URL: str | None = None
    OUTBOX_POLL_SECONDS: float = 1.0
    MESSAGE_MAX_RETRIES: int = 3
    MESSAGE_RETRY_DELAY_MS: int = 5000

    # Notificações ao cliente (orçamento disponível).
    NOTIFICATION_CHANNEL: str = "auto"
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str = "no-reply@autogiro.com.br"
    SMTP_USE_TLS: bool = True
    SMTP_TIMEOUT: int = 10

    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "*"  # lista separada por vírgula; use origens explícitas em produção

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()
