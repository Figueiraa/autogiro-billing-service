# AutoGiro · Billing Service

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

## Como rodar

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

pytest                                  # testes + cobertura (mínimo de 80%)
ruff check app tests scripts            # lint
bandit -r app -ll                       # análise de segurança

cp .env.example .env                    # defina o JWT_SECRET
docker compose up --build               # API em http://localhost:8002 (Swagger em /docs)
```

O `JWT_SECRET` é obrigatório e não tem valor padrão no código: o serviço não sobe sem ele.

## Pipeline (`.github/workflows/ci-cd.yml`)

| Job | O que faz | Bloqueia o merge? |
|---|---|---|
| **Lint & Test** | ruff, bandit e pytest com cobertura mínima de 80% | Sim |
| **SonarCloud** | Análise de qualidade com quality gate bloqueante | Sim |
| **Build & Push image** | Imagem Docker + scan de vulnerabilidades (trivy). Na `main`, publica no GHCR para amd64 e arm64 | Sim |

A `main` é protegida: só recebe código por pull request, com as três checagens verdes.

## Repositórios da plataforma

| Repositório | Papel |
|---|---|
| [autogiro-os-service](https://github.com/Figueiraa/autogiro-os-service) | Atendimento e orquestração da Saga |
| [autogiro-billing-service](https://github.com/Figueiraa/autogiro-billing-service) | Orçamento, pagamento (Mercado Pago) e estorno |
| [autogiro-execution-service](https://github.com/Figueiraa/autogiro-execution-service) | Estoque de peças e fila de execução |
| [autogiro-auth](https://github.com/Figueiraa/autogiro-auth) | Lambda de autenticação por CPF |
| [autogiro-infra-k8s](https://github.com/Figueiraa/autogiro-infra-k8s) | Cluster EKS, Kong, RabbitMQ e New Relic |
