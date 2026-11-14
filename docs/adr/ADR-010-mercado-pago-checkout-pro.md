# ADR-010: Pagamento com Mercado Pago Checkout Pro e confirmação por consulta

- **Status:** Aceita
- **Data:** 2026-11-08
- **Decisores:** Pedro Figueira

## Contexto

A plataforma precisa cobrar o orçamento aprovado pelo Mercado Pago. Duas restrições do
ambiente pesam:

1. A conta AWS **não permite criar Load Balancer**: o Kong é exposto por NodePort, em HTTP,
   no IP público de um nó. **Esse IP muda a cada recriação do cluster**, e o cluster é
   destruído ao fim de cada sessão de uso.
2. Webhooks precisam de uma URL pública estável, cadastrada no Mercado Pago.

Também não há front-end próprio: o cliente interage pela API.

## Decisão

| Aspecto | Escolha |
|---|---|
| Produto | **Checkout Pro**, pela **API de Preferences**: o Billing cria uma *preferência* e devolve o link de pagamento. O cliente paga numa página do próprio Mercado Pago, e o Billing não lida com dados de cartão |
| Link | Campo `init_point` da preferência (configurável por `MERCADOPAGO_CHECKOUT_LINK`; `sandbox_init_point` só para credenciais `TEST-` antigas) |
| Vínculo | `external_reference` única por pagamento, `autogiro-os-<order_id>-<12 hex>`, enviada na preferência e usada na busca |
| Confirmação | **Consulta periódica** a cada `PAYMENT_POLL_SECONDS` (padrão 15 s): `GET /v1/payments/search?external_reference=…`. O endpoint `POST /api/v1/payments/{order_id}/sync` força a consulta na hora |
| Expiração | A preferência é criada com `expires: true` e `expiration_date_to` = aprovação + `PAYMENT_TIMEOUT_MINUTES`. Vencido o prazo sem pagamento, o Billing emite `PagamentoExpirado` e encerra o link |
| Tentativa recusada | Não encerra a cobrança: o cliente pode tentar outro cartão no mesmo link até o prazo. Vencido o prazo sem pagamento aprovado e com tentativa recusada, o Billing emite `PagamentoRecusado` |
| Meios de pagamento | Boleto e lotérica (`ticket`, `atm`) excluídos da preferência: são pagos dias depois, fora da janela de pagamento tardio |
| Pagamento tardio | Pagamentos recusados, expirados ou cancelados continuam sendo consultados por `LATE_PAYMENT_WINDOW_HOURS` (padrão 24 h). Um pagamento aprovado que aparece nesse período é informado à Saga, que manda estornar |
| Compensação | **Estorno total** (`POST /v1/payments/{id}/refunds`, chave de idempotência fixa por pagamento) quando o pagamento já foi aprovado; encerramento do link quando ainda não foi |
| Erros do gateway | Cada falha vira `PaymentGatewayError` com `retryable`: rede, tempo esgotado, HTTP 5xx e 429 são temporárias; os demais 4xx são recusa definitiva |
| Estorno que falha | O pagamento fica `ESTORNO_PENDENTE` e a compensação é confirmada para a Saga não travar. Falha temporária: o monitor refaz o estorno. Recusa definitiva: o monitor concilia quando o estorno manual aparece no Mercado Pago |
| Ambiente | **Sandbox**: credenciais de teste, vendedor e comprador de teste, cartões de teste que forçam aprovação ou recusa |
| Testes automatizados | O Mercado Pago fica atrás da porta `PaymentGatewayPort`; o CI usa um dublê e **não depende do sandbox** |

## Como ficou implementado

O adaptador está em
[`app/infrastructure/gateways/mercadopago.py`](../../app/infrastructure/gateways/mercadopago.py)
e usa quatro endpoints:

| Endpoint | Uso |
|---|---|
| `POST /checkout/preferences` | Cria a cobrança: um item com o título da OS, quantidade 1, valor do orçamento em `BRL`, `external_reference`, expiração e `excluded_payment_types` (`ticket`, `atm`). Chave de idempotência `preferencia-<external_reference>` |
| `PUT /checkout/preferences/{id}` | Encerra o link (`expiration_date_to` = agora) quando o prazo de pagamento vence ou a cobrança é cancelada. Melhor esforço: a falha só é registrada em log |
| `GET /v1/payments/search` | Busca os pagamentos da referência, do mais recente para o mais antigo. Um aprovado prevalece sobre tentativas recusadas |
| `POST /v1/payments/{id}/refunds` | Estorno total, com chave de idempotência `estorno-<external_reference>` |

O e-mail do cliente **não** é enviado como pagador na preferência: no sandbox, quem paga é a
conta de comprador de teste, e um pagador diferente bloquearia o pagamento.

Status do Mercado Pago considerados pelo Billing
([`payment_gateway.py`](../../app/application/ports/payment_gateway.py)):

| Status no Mercado Pago | Leitura no Billing |
|---|---|
| `approved` | Pagamento aprovado |
| `rejected`, `cancelled` | Pagamento recusado |
| `refunded`, `charged_back` | Estorno concluído (conciliação do estorno pendente) |
| Demais (`pending`, `in_process`…) | Ainda em andamento: nada muda |

O tratamento de `retryable` nos casos de uso
([`billing.py`](../../app/application/use_cases/billing.py)):

- **Aprovação:** qualquer falha do gateway desfaz a transação e a API responde `502`. O
  orçamento continua `PENDENTE`.
- **Consulta (monitor e `/sync`):** a falha é registrada e o pagamento é consultado de novo no
  ciclo seguinte. A expiração também espera uma consulta bem-sucedida.
- **Estorno:** qualquer falha leva o pagamento a `ESTORNO_PENDENTE` e confirma a compensação
  com `estorno_pendente: true` (com a Saga já compensada, um comando na DLQ não seria
  reenviado). Falha temporária: o monitor refaz o estorno com a mesma chave de idempotência.
  Recusa definitiva: não é repetida; o log registra `ESTORNO MANUAL NECESSÁRIO`.

### API de Preferences

O Mercado Pago marca a API de Preferences como legada: ela continua suportada, mas as novidades
do Checkout Pro saem na API de Orders. Como o Billing só conhece a `PaymentGatewayPort`, uma
migração futura fica restrita ao adaptador, sem tocar no orçamento, no pagamento nem na Saga.

### Sandbox

Comportamentos verificados no sandbox, que mudaram a forma de testar e o tratamento do estorno:

- As credenciais de teste da aplicação (`APP_USR-…`) pertencem a um **vendedor de teste**.
  Pagar como visitante falha. O pagamento exige **entrar no checkout com um comprador de
  teste** (painel → *Contas de teste*) e usar um cartão de teste do painel, com o status
  desejado no nome do titular (`APRO` aprovado, `OTHE` recusado, `CONT` pendente).
- A API de estornos responde `401 Unauthorized use of live credentials` com essas credenciais.
  O Billing trata como recusa definitiva: o pagamento fica `ESTORNO_PENDENTE`, a Saga termina,
  o estorno é feito no painel do vendedor de teste e o monitor o concilia (`ESTORNADO`).

## Consequências

**Positivas**

- Funciona com o cluster recriado a cada sessão, sem URL fixa.
- O cenário de recusa é reproduzível com cartão de teste, e o de estorno com
  `EXECUTION_QUEUE_CAPACITY=0` no Execution Service (a OS é paga e depois recusada na fila).
- O Billing não lida com dados de cartão: a página é do Mercado Pago.
- Uma recusa definitiva do estorno não trava a Saga.

**Negativas**

- Até `PAYMENT_POLL_SECONDS` entre o pagamento e a confirmação (mitigado pelo `/sync`).
- Consultas periódicas ao Mercado Pago enquanto houver pagamento pendente, estorno pendente ou
  pagamento expirado ou cancelado dentro da janela de pagamento tardio.
- No sandbox atual, o estorno depende de um passo manual no painel. O estorno pendente fica
  visível no status do pagamento e no log.
- Uma tentativa recusada só vira `PagamentoRecusado` quando o prazo de pagamento vence: a
  Saga espera até `PAYMENT_TIMEOUT_MINUTES` para cancelar a OS.
- Sem boleto nem lotérica: só meios que confirmam dentro do prazo.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Webhook | Exige URL pública estável; a do Kong muda a cada cluster. Pode ser adicionado como evolução com um túnel (por exemplo, `cloudflared`) |
| API de Pagamentos com Pix | O QR Code de teste não tem como ser "pago" e recusado de forma controlada no sandbox tão facilmente quanto os cartões de teste |
| Checkout Transparente (cartão na nossa API) | Exige tokenizar o cartão num front-end com o SDK do Mercado Pago; não há front-end |
| API de Orders | Sucessora da de Preferences no Checkout Pro; não adotada nesta versão. A porta `PaymentGatewayPort` deixa a troca restrita ao adaptador |
