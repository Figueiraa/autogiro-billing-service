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
