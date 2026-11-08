-- Schema do Billing Service: orçamentos, pagamentos, outbox e mensagens processadas.
--
-- order_id é o id da OS no OS Service: sem chave estrangeira para dados de outro
-- serviço. Os dados do cliente e os preços são cópias recebidas no comando da Saga.

CREATE TABLE IF NOT EXISTS budgets (
    id            SERIAL         PRIMARY KEY,
    order_id      INTEGER        NOT NULL,
    saga_id       VARCHAR(36)    NOT NULL,
    client_id     INTEGER,
    client_name   VARCHAR(255),
    client_email  VARCHAR(255),
    total         NUMERIC(10, 2) NOT NULL,
    status        VARCHAR(20)    NOT NULL,
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,
    decided_at    TIMESTAMPTZ,
    CONSTRAINT uq_budgets_order UNIQUE (order_id),
    CONSTRAINT ck_budgets_total CHECK (total > 0),
    CONSTRAINT ck_budgets_status CHECK (status IN ('PENDENTE', 'APROVADO', 'RECUSADO', 'EXPIRADO', 'CANCELADO'))
);

CREATE TABLE IF NOT EXISTS budget_lines (
    id            SERIAL         PRIMARY KEY,
    budget_id     INTEGER        NOT NULL REFERENCES budgets (id) ON DELETE CASCADE,
    kind          VARCHAR(10)    NOT NULL,
    reference_id  INTEGER        NOT NULL,
    description   VARCHAR(255)   NOT NULL,
    quantity      INTEGER        NOT NULL,
    unit_price    NUMERIC(10, 2) NOT NULL,
    CONSTRAINT ck_budget_lines_kind CHECK (kind IN ('SERVICO', 'PECA')),
    CONSTRAINT ck_budget_lines_quantity CHECK (quantity > 0),
    CONSTRAINT ck_budget_lines_unit_price CHECK (unit_price >= 0)
);

CREATE TABLE IF NOT EXISTS payments (
    id                  SERIAL         PRIMARY KEY,
    budget_id           INTEGER        NOT NULL REFERENCES budgets (id),
    order_id            INTEGER        NOT NULL,
    saga_id             VARCHAR(36)    NOT NULL,
    amount              NUMERIC(10, 2) NOT NULL,
    status              VARCHAR(30)    NOT NULL,
    external_reference  VARCHAR(60)    NOT NULL,
    mp_preference_id    VARCHAR(100),
    payment_link        VARCHAR(500),
    mp_payment_id       VARCHAR(40),
    status_detail       VARCHAR(100),
    refund_id           VARCHAR(40),
    expires_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT uq_payments_external_reference UNIQUE (external_reference),
    CONSTRAINT ck_payments_amount CHECK (amount > 0),
    CONSTRAINT ck_payments_status CHECK (status IN (
        'PENDENTE', 'APROVADO', 'RECUSADO', 'EXPIRADO', 'CANCELADO', 'ESTORNADO', 'ESTORNO_PENDENTE'
    ))
);

-- Eventos gravados na mesma transação da mudança de estado e publicados depois.
CREATE TABLE IF NOT EXISTS outbox (
    id            SERIAL        PRIMARY KEY,
    message_id    VARCHAR(36)   NOT NULL,
    message_type  VARCHAR(60)   NOT NULL,
    exchange      VARCHAR(60)   NOT NULL,
    routing_key   VARCHAR(120)  NOT NULL,
    body          TEXT          NOT NULL,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ,
    CONSTRAINT uq_outbox_message UNIQUE (message_id)
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id    VARCHAR(36)  PRIMARY KEY,
    processed_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_payments_order ON payments (order_id);
CREATE INDEX IF NOT EXISTS ix_payments_watch ON payments (status, updated_at);
CREATE INDEX IF NOT EXISTS ix_budgets_pending ON budgets (expires_at) WHERE status = 'PENDENTE';
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox (id) WHERE published_at IS NULL;
