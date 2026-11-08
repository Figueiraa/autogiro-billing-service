"""Exceções de domínio. A camada HTTP as converte em códigos de status."""


class DomainError(Exception):
    """Base de todas as exceções de negócio do serviço."""


class NotFoundError(DomainError):
    def __init__(self, entity: str, identifier: object):
        super().__init__(f"{entity} {identifier} não encontrado(a)")
        self.entity = entity
        self.identifier = identifier


class ConflictError(DomainError):
    """Operação conflita com o estado atual (por exemplo, duplicidade)."""


class BusinessRuleError(DomainError):
    """Regra de negócio violada (por exemplo, transição de status inválida)."""


class InvalidStatusTransitionError(BusinessRuleError):
    """O orçamento ou o pagamento não pode ir do status atual para o pedido."""

    def __init__(self, entity: str, current: str, target: str):
        super().__init__(f"{entity}: transição inválida '{current}' → '{target}'")
        self.current = current
        self.target = target


class PaymentGatewayError(Exception):
    """O provedor de pagamento falhou ou não respondeu.

    `retryable` separa a falha temporária (rede, erro 5xx, limite de requisições),
    que vale a pena tentar de novo, da recusa definitiva (erro 4xx), que não muda
    com novas tentativas.
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable
