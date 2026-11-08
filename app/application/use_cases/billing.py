"""Casos de uso do Billing: orçamento, pagamento e participação na Saga.

Toda mudança grava o evento de resposta no outbox, na mesma transação.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.application.messages import Envelope
from app.application.ports.notifier import NotifierPort
from app.application.ports.payment_gateway import PaymentGatewayPort
from app.application.ports.unit_of_work import UnitOfWorkFactory, UnitOfWorkPort
from app.domain.entities.budget import Budget, BudgetStatus, InvalidBudgetError
from app.domain.entities.payment import Payment, PaymentStatus, new_external_reference
from app.domain.exceptions import BusinessRuleError, NotFoundError, PaymentGatewayError

logger = logging.getLogger("autogiro.billing")

DONE = "done"
DUPLICATE = "duplicate"
IGNORED = "ignored"


@dataclass
class BillingPolicy:
    approval_timeout: timedelta = timedelta(hours=24)
    payment_timeout: timedelta = timedelta(hours=1)
    # Por quanto tempo um pagamento expirado ou cancelado ainda é consultado: se o
    # cliente pagou no último instante, a confirmação tardia dispara o estorno.
    late_payment_window: timedelta = timedelta(hours=24)


@dataclass
class CommandResult:
    outcome: str
    event: str | None = None


@dataclass
class MonitorResult:
    events: list[str] = field(default_factory=list)


def _event(event_type: str, source, payload: dict, causation_id: str | None = None) -> Envelope:
    return Envelope.event(
        event_type,
        saga_id=source.saga_id,
        order_id=source.order_id,
        payload=payload,
        causation_id=causation_id,
    )


class BillingService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        gateway: PaymentGatewayPort,
        notifier: NotifierPort | None = None,
        policy: BillingPolicy | None = None,
    ):
        self._uow = uow_factory
        self._gateway = gateway
        self._notifier = notifier
        self._policy = policy or BillingPolicy()

    # ─── Comandos da Saga ────────────────────────────────────────────────────
    async def handle_command(self, command: Envelope) -> CommandResult:
        handlers = {"GerarOrcamento": self._generate_budget, "CancelarCobranca": self._cancel_charge}
        notify = None
        async with self._uow() as uow:
            if not await uow.inbox.register(command.message_id):
                return CommandResult(DUPLICATE)
            handler = handlers.get(command.type) if command.kind == "command" else None
            if handler is None:
                logger.warning("Mensagem %s ignorada pelo Billing", command.type)
                return CommandResult(IGNORED)
            event_type, payload, notify = await handler(uow, command)
            await uow.outbox.add(_event(event_type, command, payload, command.message_id))

        if notify is not None:
            await self._send_budget(notify)
        logger.info("OS %s: %s → %s", command.order_id, command.type, event_type)
        return CommandResult(DONE, event_type)

    async def _generate_budget(self, uow: UnitOfWorkPort, command: Envelope):
        existing = await uow.budgets.get_by_order(command.order_id)
        if existing is not None:
            return "OrcamentoGerado", {"budget_id": existing.id, "total": existing.total}, None

        payload = command.payload
        try:
            budget = Budget.generate(
                order_id=command.order_id,
                saga_id=command.saga_id,
                client=payload.get("client") or {},
                services=payload.get("services", []),
                parts=payload.get("parts", []),
                approval_timeout=self._policy.approval_timeout,
            )
        except InvalidBudgetError as exc:
            return "OrcamentoFalhou", {"reason": str(exc)}, None

        budget = await uow.budgets.add(budget)
        return "OrcamentoGerado", {"budget_id": budget.id, "total": budget.total}, budget

    async def _cancel_charge(self, uow: UnitOfWorkPort, command: Envelope):
        """Compensação: cancela o orçamento e a cobrança; estorna se já houve pagamento."""
        now = datetime.now(UTC)
        budget = await uow.budgets.get_by_order(command.order_id)
        if budget is not None and budget.cancel(now):
            await uow.budgets.save(budget)

        refunded, refund_id = False, None
        payment = await uow.payments.get_by_order(command.order_id)
        if payment is not None:
            if payment.status == PaymentStatus.PENDENTE:
                payment.cancel(now)
                await uow.payments.save(payment)
                await self._expire_link(payment)
            elif payment.status == PaymentStatus.APROVADO:
                refunded, refund_id = await self._refund(payment, now)
                await uow.payments.save(payment)
            elif payment.status == PaymentStatus.ESTORNADO:
                refunded, refund_id = True, payment.refund_id
        pending = payment is not None and payment.status == PaymentStatus.ESTORNO_PENDENTE
        payload = {"estornado": refunded, "refund_id": refund_id, "estorno_pendente": pending}
        return "CobrancaCancelada", payload, None

    async def _refund(self, payment: Payment, now: datetime) -> tuple[bool, str | None]:
        """Estorna no provedor.

        Falha temporária sobe (retry e DLQ). Recusa definitiva deixa o estorno
        pendente, para ser feito manualmente e conciliado depois, sem travar a Saga.
        """
        try:
            # Chave de idempotência fixa: repetir o comando não estorna duas vezes.
            refund_id = await self._gateway.refund(
                payment.mp_payment_id, f"estorno-{payment.external_reference}"
            )
        except PaymentGatewayError as exc:
            if exc.retryable:
                raise
            payment.refund_pending(str(exc), now)
            logger.error(
                "ESTORNO MANUAL NECESSÁRIO: pagamento %s da OS %s (R$ %.2f) — %s",
                payment.mp_payment_id,
                payment.order_id,
                payment.amount,
                exc,
            )
            return False, None
        payment.refund(refund_id, now)
        return True, refund_id

    # ─── Decisão do cliente ──────────────────────────────────────────────────
    async def get_budget(self, order_id: int) -> Budget:
        async with self._uow() as uow:
            return await self._budget(uow, order_id)

    async def approve(self, order_id: int) -> tuple[Budget, Payment]:
        """Aprova e cria a cobrança no Mercado Pago. Se ele falhar, nada é gravado."""
        now = datetime.now(UTC)
        async with self._uow() as uow:
            budget = await self._open_budget(uow, order_id, now)
            budget.approve(now)
            payment = await uow.payments.add(
                Payment(
                    budget_id=budget.id,
                    order_id=order_id,
                    saga_id=budget.saga_id,
                    amount=budget.total,
                    external_reference=new_external_reference(order_id),
                    expires_at=now + self._policy.payment_timeout,
                    updated_at=now,
                )
            )
            preference = await self._gateway.create_preference(
                external_reference=payment.external_reference,
                title=f"AutoGiro · Ordem de serviço {order_id}",
                amount=payment.amount,
                payer_email=budget.client_email,
                expires_at=payment.expires_at,
            )
            payment.mp_preference_id, payment.payment_link = preference.id, preference.link
            await uow.payments.save(payment)
            await uow.budgets.save(budget)
            await uow.outbox.add(
                _event(
                    "OrcamentoAprovado",
                    budget,
                    {
                        "budget_id": budget.id,
                        "payment_id": payment.id,
                        "payment_link": payment.payment_link,
                        "amount": payment.amount,
                    },
                )
            )
        return budget, payment

    async def reject(self, order_id: int) -> Budget:
        now = datetime.now(UTC)
        async with self._uow() as uow:
            budget = await self._open_budget(uow, order_id, now)
            budget.reject(now)
            await uow.budgets.save(budget)
            await uow.outbox.add(_event("OrcamentoRecusado", budget, {"budget_id": budget.id}))
        return budget

    # ─── Pagamento ──────────────────────────────────────────────────────────
    async def get_payment(self, order_id: int) -> Payment:
        async with self._uow() as uow:
            payment = await uow.payments.get_by_order(order_id)
        if payment is None:
            raise NotFoundError("Pagamento da OS", order_id)
        return payment

    async def sync_payment(self, order_id: int) -> Payment:
        """Consulta o Mercado Pago agora, sem esperar o próximo ciclo do monitor."""
        async with self._uow() as uow:
            payment = await uow.payments.get_by_order(order_id)
            if payment is None:
                raise NotFoundError("Pagamento da OS", order_id)
            await self._check_payment(uow, payment, datetime.now(UTC))
        return payment

    async def run_monitor(self, now: datetime | None = None) -> MonitorResult:
        """Expira orçamentos sem resposta e confere os pagamentos em andamento."""
        moment = now or datetime.now(UTC)
        result = MonitorResult()

        async with self._uow() as uow:
            for budget in await uow.budgets.list_expired_pending(moment):
                budget.expire(moment)
                await uow.budgets.save(budget)
                await uow.outbox.add(_event("OrcamentoExpirado", budget, {"budget_id": budget.id}))
                result.events.append("OrcamentoExpirado")
            watched = await uow.payments.list_to_watch(moment - self._policy.late_payment_window)

        for candidate in watched:
            async with self._uow() as uow:
                payment = await uow.payments.get_by_order(candidate.order_id)
                if payment is not None and payment.id == candidate.id:
                    event = await self._check_payment(uow, payment, moment)
                    if event:
                        result.events.append(event)
        return result

    async def _check_payment(self, uow: UnitOfWorkPort, payment: Payment, now: datetime) -> str | None:
        try:
            found = await self._gateway.find_payment(payment.external_reference)
        except PaymentGatewayError as exc:
            logger.warning("Consulta do pagamento %s falhou: %s", payment.id, exc)
            return None

        if payment.status == PaymentStatus.ESTORNO_PENDENTE:
            return await self._reconcile_refund(uow, payment, found, now)

        late = payment.status in (PaymentStatus.EXPIRADO, PaymentStatus.CANCELADO)
        if found is not None and found.approved and (payment.status == PaymentStatus.PENDENTE or late):
            payment.confirm(found.id, now)
            event_type = "PagamentoConfirmado"
            payload = {"payment_id": payment.id, "mp_payment_id": found.id, "amount": payment.amount}
            if late:
                logger.warning("Pagamento tardio na OS %s: a Saga vai estornar", payment.order_id)
        elif found is not None and found.rejected and payment.status == PaymentStatus.PENDENTE:
            payment.reject(found.id, found.status_detail, now)
            event_type = "PagamentoRecusado"
            payload = {
                "payment_id": payment.id,
                "mp_payment_id": found.id,
                "status_detail": found.status_detail,
            }
            await self._expire_link(payment)
        elif payment.is_expired(now):
            payment.expire(now)
            event_type = "PagamentoExpirado"
            payload = {"payment_id": payment.id}
            await self._expire_link(payment)
        else:
            return None

        await uow.payments.save(payment)
        await uow.outbox.add(_event(event_type, payment, payload))
        return event_type

    async def _reconcile_refund(
        self, uow: UnitOfWorkPort, payment: Payment, found, now: datetime
    ) -> str | None:
        """Estorno feito manualmente no painel do Mercado Pago: registra quando ele aparece."""
        if found is None or not found.refunded:
            return None
        payment.refund("manual", now)
        await uow.payments.save(payment)
        logger.info("Estorno manual do pagamento %s conciliado", payment.mp_payment_id)
        return "EstornoConciliado"

    # ─── Auxiliares ─────────────────────────────────────────────────────────
    async def _open_budget(self, uow: UnitOfWorkPort, order_id: int, now: datetime) -> Budget:
        budget = await self._budget(uow, order_id)
        if budget.is_expired(now):
            raise BusinessRuleError("O prazo para responder este orçamento terminou")
        if budget.status != BudgetStatus.PENDENTE:
            raise BusinessRuleError(f"O orçamento já foi respondido ({budget.status.value})")
        return budget

    @staticmethod
    async def _budget(uow: UnitOfWorkPort, order_id: int) -> Budget:
        budget = await uow.budgets.get_by_order(order_id)
        if budget is None:
            raise NotFoundError("Orçamento da OS", order_id)
        return budget

    async def _expire_link(self, payment: Payment) -> None:
        if not payment.mp_preference_id:
            return
        try:
            await self._gateway.expire_preference(payment.mp_preference_id)
        except PaymentGatewayError as exc:  # melhor esforço: o estorno cobre um pagamento tardio
            logger.warning("Não foi possível expirar o link do pagamento %s: %s", payment.id, exc)

    async def _send_budget(self, budget: Budget) -> None:
        if self._notifier is None:
            return
        total = f"{budget.total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        await self._notifier.notify_status_change(
            budget.client_email, str(budget.order_id), f"AGUARDANDO_APROVACAO (orçamento de R$ {total})"
        )
