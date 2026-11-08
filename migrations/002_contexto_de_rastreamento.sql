-- Contexto do trace (cabeçalhos W3C) em que cada evento do outbox foi criado.
-- O publicador o envia junto com o evento, para o trace continuar no serviço
-- que o consome.

ALTER TABLE outbox ADD COLUMN IF NOT EXISTS trace_context TEXT;
