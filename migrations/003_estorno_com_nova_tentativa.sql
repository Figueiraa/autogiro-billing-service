-- Estorno pendente que vale tentar de novo.
--
-- Uma falha temporária do Mercado Pago no estorno (rede, 5xx, 429) deixa o
-- pagamento ESTORNO_PENDENTE com esta marca, e o monitor repete o estorno. Uma
-- recusa definitiva (4xx) fica sem a marca: só espera o estorno manual.

ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_retryable BOOLEAN NOT NULL DEFAULT FALSE;
