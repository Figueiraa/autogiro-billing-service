"""Adaptador do Mercado Pago: Checkout Pro com a API de Preferences.

Endpoints usados:
    POST /checkout/preferences            cria a cobrança e devolve o link de pagamento
    PUT  /checkout/preferences/{id}       encerra o link (expiração imediata)
    GET  /v1/payments/search              consulta pagamentos pela referência externa
    POST /v1/payments/{id}/refunds        estorno total

A API de Preferences é marcada como legada pelo Mercado Pago (continua suportada;
novidades só na API de Orders). A troca, quando vier, fica restrita a este arquivo.
"""

from datetime import UTC, datetime

import httpx

from app.application.ports.payment_gateway import GatewayPayment, PaymentGatewayPort, Preference
from app.domain.exceptions import PaymentGatewayError


def _iso(moment: datetime) -> str:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.isoformat(timespec="milliseconds")


class MercadoPagoGateway(PaymentGatewayPort):
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = "https://api.mercadopago.com",
        checkout_link_field: str = "init_point",
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._base_url = base_url.rstrip("/")
        self._link_field = checkout_link_field
        self._timeout = timeout_seconds
        self._client = client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        idempotency_key=None,
    ) -> dict:
        headers = dict(self._headers)
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        url = f"{self._base_url}{path}"
        try:
            if self._client is not None:
                response = await self._client.request(
                    method, url, json=json, params=params, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.request(
                        method, url, json=json, params=params, headers=headers, timeout=self._timeout
                    )
        except httpx.HTTPError as exc:
            raise PaymentGatewayError(f"Mercado Pago indisponível: {exc}") from exc

        if response.status_code >= 400:
            try:
                detail = response.json().get("message") or response.text
            except ValueError:
                detail = response.text
            retryable = response.status_code >= 500 or response.status_code == 429
            raise PaymentGatewayError(f"Mercado Pago respondeu {response.status_code}: {detail}", retryable)
        return response.json()

    async def create_preference(
        self,
        *,
        external_reference: str,
        title: str,
        amount: float,
        payer_email: str | None,
        expires_at: datetime,
    ) -> Preference:
        # O e-mail do cliente não é enviado como pagador: no sandbox, quem paga é a conta
        # de teste compradora, e um pagador diferente bloquearia o pagamento.
        data = await self._request(
            "POST",
            "/checkout/preferences",
            json={
                "items": [
                    {
                        "id": external_reference,
                        "title": title,
                        "quantity": 1,
                        "unit_price": round(amount, 2),
                        "currency_id": "BRL",
                    }
                ],
                "external_reference": external_reference,
                # Boleto e pagamento em lotérica são pagos dias depois, fora da janela de
                # pagamento tardio: o dinheiro entraria sem ninguém para estorná-lo.
                "payment_methods": {"excluded_payment_types": [{"id": "ticket"}, {"id": "atm"}]},
                "expires": True,
                "expiration_date_to": _iso(expires_at),
                # Prazo dos pagamentos em dinheiro: se algum escapar do filtro acima, ele
                # vence junto com o link.
                "date_of_expiration": _iso(expires_at),
            },
            idempotency_key=f"preferencia-{external_reference}",
        )
        return Preference(id=data["id"], link=data.get(self._link_field) or data["init_point"])

    async def find_payment(self, external_reference: str) -> GatewayPayment | None:
        data = await self._request(
            "GET",
            "/v1/payments/search",
            params={"external_reference": external_reference, "sort": "date_created", "criteria": "desc"},
        )
        payments = [
            GatewayPayment(id=str(p["id"]), status=p["status"], status_detail=p.get("status_detail"))
            for p in data.get("results", [])
        ]
        if not payments:
            return None
        # Um pagamento aprovado prevalece sobre tentativas recusadas da mesma cobrança.
        return next((p for p in payments if p.approved), payments[0])

    async def refund(self, mp_payment_id: str, idempotency_key: str) -> str:
        data = await self._request(
            "POST", f"/v1/payments/{mp_payment_id}/refunds", json={}, idempotency_key=idempotency_key
        )
        return str(data["id"])

    async def expire_preference(self, preference_id: str) -> None:
        await self._request(
            "PUT",
            f"/checkout/preferences/{preference_id}",
            json={"expiration_date_to": _iso(datetime.now(UTC))},
        )


class DisabledPaymentGateway(PaymentGatewayPort):
    """Usado sem `MERCADOPAGO_ACCESS_TOKEN`: toda operação falha com uma mensagem clara."""

    async def _fail(self, *_args, **_kwargs):
        raise PaymentGatewayError("MERCADOPAGO_ACCESS_TOKEN não configurado")

    create_preference = _fail
    find_payment = _fail
    refund = _fail
    expire_preference = _fail
