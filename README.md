# AutoGiro · Billing Service

[![CI/CD](https://github.com/Figueiraa/autogiro-billing-service/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/Figueiraa/autogiro-billing-service/actions/workflows/ci-cd.yml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=Figueiraa_autogiro-billing-service&metric=alert_status)](https://sonarcloud.io/summary/overall?id=Figueiraa_autogiro-billing-service)
[![Cobertura](https://sonarcloud.io/api/project_badges/measure?project=Figueiraa_autogiro-billing-service&metric=coverage)](https://sonarcloud.io/component_measures?id=Figueiraa_autogiro-billing-service&metric=coverage)

> Microsserviço de **Cobrança** da plataforma AutoGiro, para redes de oficinas mecânicas.
> Tech Challenge **Fase 4** (13SOAT): arquitetura de microsserviços com Saga Pattern.

| | |
|---|---|
| **É dono de** | Orçamentos, pagamentos e estornos |
| **Papel na Saga** | Participante: gera orçamento, cobra pelo Mercado Pago e estorna na compensação |
| **Banco** | PostgreSQL (Neon) |
| **Comunicação** | REST pelo Kong (borda) · RabbitMQ (comandos e eventos da Saga) |

## Estrutura

Clean Architecture (domínio, aplicação, infraestrutura e interfaces), sondas de saúde,
métricas Prometheus, logs JSON, validação do JWT emitido pelo `autogiro-auth` e pipeline com
testes, SonarCloud e imagem Docker.

## API

Todas as rotas exigem o JWT do `autogiro-auth`. Documentação interativa em `/docs` (Swagger).

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/v1/budgets/{order_id}` | Orçamento da OS, com serviços e peças |
| `POST` | `/api/v1/budgets/{order_id}/approve` | Aprova e devolve o **link de pagamento** (Checkout Pro). `502` se o Mercado Pago falhar: o orçamento continua pendente |
| `POST` | `/api/v1/budgets/{order_id}/reject` | Recusa: a Saga libera as peças e cancela a OS |
| `GET` | `/api/v1/payments/{order_id}` | Situação do pagamento |
| `POST` | `/api/v1/payments/{order_id}/sync` | Consulta o Mercado Pago agora, sem esperar o ciclo de 15 s |

## Participação na Saga

O orquestrador fica no OS Service. Este serviço recebe comandos na fila `billing.commands` e
responde com eventos no exchange `autogiro.events`:

| Origem | Evento |
|---|---|
| Comando `GerarOrcamento` | `OrcamentoGerado` (com o total) ou `OrcamentoFalhou` (preço ausente, total zero) |
| Cliente aprova / recusa | `OrcamentoAprovado` (com o link de pagamento) / `OrcamentoRecusado` |
| Monitor: prazo de resposta esgotado | `OrcamentoExpirado` |
| Monitor: consulta ao Mercado Pago | `PagamentoConfirmado`, `PagamentoRecusado` ou `PagamentoExpirado` |
| Comando `CancelarCobranca` (compensação) | `CobrancaCancelada`, com `estornado` e `estorno_pendente` |

- **Idempotência:** orçamento único por OS; a mesma mensagem entregue duas vezes é descartada.
- **Outbox:** cada mudança e o evento de resposta são gravados na mesma transação.
- **Bloqueio:** aprovação e cancelamento simultâneos do mesmo orçamento não se sobrepõem
  (`SELECT … FOR UPDATE`).

## Pagamento (Mercado Pago)

Checkout Pro com a API de Preferences: o cliente paga numa página do próprio Mercado Pago,
então o Billing não lida com dados de cartão.

1. **Aprovação:** cria a *preferência* com o valor do orçamento, prazo de expiração e uma
   `external_reference` única, e devolve o link de pagamento.
2. **Confirmação por consulta:** a cada 15 s, o monitor busca os pagamentos da referência. Não
   depende de webhook, porque o endereço público do Kong muda a cada recriação do cluster.
3. **Pagamento tardio:** um pagamento que chega depois de a cobrança expirar ou ser cancelada
   ainda é detectado (janela de 24 h) e informado à Saga, que manda estornar.
4. **Estorno (compensação):** estorno total com chave de idempotência; repetir o comando não
   estorna duas vezes.
   - Falha **temporária** (rede, erro 5xx, limite de requisições): nova tentativa e, depois, DLQ.
   - Recusa **definitiva** (erro 4xx): o pagamento fica `ESTORNO_PENDENTE`, a compensação é
     confirmada para a Saga não travar e o log registra `ESTORNO MANUAL NECESSÁRIO`. Quando o
     estorno feito no painel aparece no Mercado Pago, o monitor concilia e o pagamento vira
     `ESTORNADO`.

A integração fica atrás da porta `PaymentGatewayPort`: os testes usam um dublê e não dependem do
sandbox. A API de Preferences é marcada como legada pelo Mercado Pago (continua suportada); a
migração para a API de Orders fica restrita ao adaptador `app/infrastructure/gateways/mercadopago.py`.

### Testar no sandbox

- As credenciais de teste da aplicação pertencem a um **vendedor de teste**: quem paga precisa
  ser outro usuário de teste. No painel, em *Contas de teste*, crie um **comprador** e entre
  com ele no link de pagamento (*Com sua conta Mercado Pago*).
- Cartões de teste: os do painel (*Cartões de teste*), com o status desejado no nome do titular:
  `APRO` (aprovado), `OTHE` (recusado), `CONT` (pendente). CPF `12345678909`.
- No sandbox atual, a API recusa o estorno com credenciais de vendedor de teste
  (`401 Unauthorized use of live credentials`). O fluxo cai no estorno pendente descrito acima.

## Observabilidade

Métricas Prometheus em `GET /metrics` (raspadas pelo agente do New Relic no cluster) e
**rastreamento distribuído com OpenTelemetry**, exportado direto para o New Relic por OTLP.

| Variável | Uso |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `https://otlp.nr-data.net`. Sem ela, nada é exportado e o serviço funciona igual |
| `NEW_RELIC_LICENSE_KEY` | License key de ingestão, enviada no cabeçalho `api-key` |
| `DEPLOYMENT_ENVIRONMENT` | Ambiente nos traces (`local`, `producao`) |

Como uma OS vira **um trace só** entre os serviços:

- HTTP de entrada e de saída (httpx, inclusive o Mercado Pago) e o PostgreSQL (SQLAlchemy) são instrumentados automaticamente; as chamadas HTTP levam
  o cabeçalho `traceparent` (W3C).
- Cada mensagem gravada no outbox guarda o contexto do trace em que nasceu. O publicador envia esse
  contexto nos cabeçalhos AMQP e o consumidor continua o mesmo trace. Assim o trace segue o fluxo
  de negócio, e não o laço que publica o outbox.
- Spans de mensagem levam `autogiro.saga_id`, `autogiro.order_id`, `autogiro.message.type` e o
  desfecho da entrega (`done`, `retry` ou `dead_letter`).
- Os logs JSON levam `trace.id` e `span.id` (nomes que o New Relic usa para ligar log e trace) e,
  durante o processamento de uma mensagem, `saga_id`, `order_id` e `message_type`.
- Consultas ao banco feitas pelos laços em segundo plano (outbox, monitor de pagamentos, sondas de saúde) não abrem
  trace próprio: só aparecem dentro de uma requisição ou mensagem.

O painel **AutoGiro — Saga e rastreamento** e o alerta de mensagem na DLQ ficam no
[autogiro-infra-k8s](https://github.com/Figueiraa/autogiro-infra-k8s), em Terraform.

## Banco de dados

PostgreSQL próprio, num projeto Neon só do serviço (`terraform/`). Postgres porque é dinheiro:
aprovar o orçamento e criar o pagamento acontecem numa transação ACID. O schema fica em
`migrations/` e é aplicado por `python -m app.infrastructure.migrations`.

| Tabela | Conteúdo |
|---|---|
| `budgets`, `budget_lines` | Orçamento por OS, com serviços e peças (preços copiados do comando) |
| `payments` | Cobranças no Mercado Pago, com o status e o estorno |
| `outbox` | Eventos a publicar |
| `processed_messages` | Comandos já processados |

## Como rodar

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

pytest                                  # testes + cobertura (mínimo de 80%)
ruff check app tests scripts            # lint
bandit -r app -ll                       # análise de segurança

cp .env.example .env                    # defina JWT_SECRET e MERCADOPAGO_ACCESS_TOKEN
docker compose up --build               # aplica as migrations e sobe a API em http://localhost:8002
```

Os testes de integração com PostgreSQL e RabbitMQ reais rodam quando `TEST_POSTGRES_URL` e
`TEST_RABBITMQ_URL` estão definidas (no CI, containers dos dois):

```bash
TEST_POSTGRES_URL=postgresql+asyncpg://autogiro:autogiro@localhost:5434/autogiro_billing \
TEST_RABBITMQ_URL=amqp://guest:guest@localhost:5673/ pytest
```

O `JWT_SECRET` é obrigatório e não tem valor padrão no código: o serviço não sobe sem ele.

## Pipeline (`.github/workflows/ci-cd.yml`)

| Job | O que faz | Bloqueia o merge? |
|---|---|---|
| **Lint & Test** | ruff, bandit e pytest com cobertura mínima de 80% | Sim |
| **SonarCloud** | Análise de qualidade com quality gate bloqueante | Sim |
| **Build & Push image** | Imagem Docker + scan de vulnerabilidades (trivy). Na `main`, publica no GHCR para amd64 e arm64 | Sim |

A `main` é protegida: só recebe código por pull request, com as três checagens verdes.

## Qualidade e cobertura

- **Painel do SonarCloud:** <https://sonarcloud.io/summary/overall?id=Figueiraa_autogiro-billing-service> (quality gate, cobertura, bugs, vulnerabilidades e code smells).
- **Relatório de cada execução:** aba *Summary* da pipeline no GitHub Actions, com o quadro de testes e cobertura, e o artefato `test-and-coverage-reports` (JUnit, `coverage.xml` e HTML).

## Repositórios da plataforma

| Repositório | Papel |
|---|---|
| [autogiro-os-service](https://github.com/Figueiraa/autogiro-os-service) | Atendimento e orquestração da Saga |
| [autogiro-billing-service](https://github.com/Figueiraa/autogiro-billing-service) | Orçamento, pagamento (Mercado Pago) e estorno |
| [autogiro-execution-service](https://github.com/Figueiraa/autogiro-execution-service) | Estoque de peças e fila de execução |
| [autogiro-auth](https://github.com/Figueiraa/autogiro-auth) | Lambda de autenticação por CPF |
| [autogiro-infra-k8s](https://github.com/Figueiraa/autogiro-infra-k8s) | Cluster EKS, Kong, RabbitMQ e New Relic |
