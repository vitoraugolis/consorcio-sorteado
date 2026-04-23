"""
services/zapsign.py — Cliente assíncrono para ZapSign (assinatura de contratos)

Fluxo:
  1. Lead aceita proposta → card move para "Aceito"
  2. jobs/contrato.py chama ZapSignClient.create_from_template()
  3. ZapSign cria o documento e retorna sign_url de cada signatário
  4. Sistema envia sign_url ao lead via WhatsApp
  5. Signatários internos são notificados
  6. Quando todos assinam → ZapSign chama webhook /webhook/zapsign
  7. Webhook move card para "Sucesso" e notifica agente humano

Documentação ZapSign: https://docs.zapsign.com.br
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ZAPSIGN_TOKEN   = os.environ["ZAPSIGN_TOKEN"]
ZAPSIGN_BASE    = "https://api.zapsign.com.br/api/v1"

# Mapeamento de administradora → template_token
# Bradesco, Porto e Sicoob compartilham o mesmo template
TEMPLATE_BY_ADM: dict[str, str] = {
    "santander":  "6c143118-62ec-4a3d-85c3-09eee5465bf5",
    "itaú":       "8fdb67d7-a01b-45c5-afeb-c152eccd030f",
    "itau":       "8fdb67d7-a01b-45c5-afeb-c152eccd030f",
    "caixa":      "c50dd4fd-4515-46eb-8beb-962ef87f4140",
    "embracon":   "f6b4dd2a-efd2-4a8c-a25e-d711c65eb7e0",
    "bradesco":   "9c9498d3-c8b1-4a81-9e98-c68534be7429",
    "porto":      "9c9498d3-c8b1-4a81-9e98-c68534be7429",
    "sicoob":     "9c9498d3-c8b1-4a81-9e98-c68534be7429",
}

# Signatários internos (configurados via .env)
# ZAPSIGN_INTERNAL_SIGNERS=Nome1:email1@empresa.com,Nome2:email2@empresa.com
def _parse_internal_signers() -> list[dict]:
    raw = os.getenv("ZAPSIGN_INTERNAL_SIGNERS", "")
    signers = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            name, email = entry.split(":", 1)
            signers.append({"name": name.strip(), "email": email.strip()})
    return signers

INTERNAL_SIGNERS = _parse_internal_signers()


class ZapSignError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class ZapSignClient:
    """
    Cliente assíncrono para a API ZapSign.

    Uso:
        async with ZapSignClient() as zap:
            doc = await zap.create_from_template(
                template_token="...",
                doc_name="Contrato - João Silva",
                lead_signer={"name": "João Silva", "email": "joao@email.com", "phone": "5511999999999"},
                form_fields={"nome_completo": "João Silva", "cpf": "123.456.789-00", ...}
            )
            sign_url = doc["lead_sign_url"]
    """

    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=ZAPSIGN_BASE,
            headers={
                "Authorization": f"Bearer {ZAPSIGN_TOKEN}",
                "Content-Type":  "application/json",
            },
            timeout=30.0,
        )

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    async def _post(self, endpoint: str, body: dict) -> dict:
        try:
            r = await self._client.post(endpoint, json=body)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ZapSignError(
                f"HTTP {e.response.status_code} em {endpoint}: {e.response.text[:300]}",
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise ZapSignError(f"Erro de rede em {endpoint}: {e}") from e

    async def _get(self, endpoint: str) -> dict:
        try:
            r = await self._client.get(endpoint)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ZapSignError(
                f"HTTP {e.response.status_code} em {endpoint}: {e.response.text[:300]}",
                status_code=e.response.status_code,
            ) from e

    # ------------------------------------------------------------------
    # Inspeção de templates (use para descobrir variable_name dos campos)
    # ------------------------------------------------------------------

    async def get_template(self, template_token: str) -> dict:
        """
        Retorna os detalhes de um template, incluindo:
          - signers: lista de signatários configurados
          - form_fields: lista com variable_name, label e type de cada campo

        Use este método para inspecionar os campos disponíveis em cada template.
        Exemplo de uso:
            async with ZapSignClient() as zap:
                t = await zap.get_template("6c143118-...")
                for f in t["form_fields"]:
                    print(f["variable_name"], "→", f["label"])
        """
        return await self._get(f"/models/{template_token}/")

    async def list_all_templates(self) -> list[dict]:
        """Lista todos os templates disponíveis na conta."""
        data = await self._get("/models/")
        return data if isinstance(data, list) else data.get("results", [])

    # ------------------------------------------------------------------
    # Criação de documento
    # ------------------------------------------------------------------

    async def create_from_template(
        self,
        template_token: str,
        doc_name: str,
        lead_signer: dict,
        form_fields: dict[str, str] | None = None,
        extra_signers: list[dict] | None = None,
    ) -> dict:
        """
        Cria um documento a partir de um template ZapSign.

        Args:
            template_token: Token do template (use TEMPLATE_BY_ADM para mapear).
            doc_name: Nome do documento (aparece para o signatário).
            lead_signer: Dict com dados do lead:
                {
                    "name": "João Silva",
                    "email": "joao@email.com",      # opcional mas recomendado
                    "phone": "5511999999999",        # apenas dígitos, com DDD+DDI
                }
            form_fields: Dict {variable_name: value} para preencher o template.
                Os variable_name dependem de cada template — use get_template() para descobrir.
            extra_signers: Signatários adicionais além do lead e dos internos.

        Returns:
            Dict com:
                {
                    "doc_token": str,           # token do documento
                    "open_id": int,             # ID numérico do documento
                    "lead_sign_url": str,       # link de assinatura para enviar ao lead
                    "internal_sign_urls": list, # links dos signatários internos
                    "all_signers": list,        # lista completa de signatários com tokens
                }
        """
        # Monta lista de signatários: lead + internos + extras
        signers: list[dict[str, Any]] = []

        # 1. Lead (sempre o primeiro)
        lead: dict[str, Any] = {
            "name": lead_signer["name"],
            "lock_name": True,
            "send_automatic_email": False,    # enviamos via WhatsApp manualmente
            "send_automatic_whatsapp": False,
        }
        if lead_signer.get("email"):
            lead["email"] = lead_signer["email"]
            lead["lock_email"] = True
        if lead_signer.get("phone"):
            phone = lead_signer["phone"]
            # Separa DDI + número (ZapSign espera phone_country e phone_number separados)
            if phone.startswith("55") and len(phone) >= 12:
                lead["phone_country"] = "55"
                lead["phone_number"]  = phone[2:]
        signers.append(lead)

        # 2. Signatários internos (da empresa)
        for internal in (INTERNAL_SIGNERS or []):
            signers.append({
                "name":                   internal["name"],
                "email":                  internal["email"],
                "send_automatic_email":   False,
                "send_automatic_whatsapp": False,
            })

        # 3. Signatários extras opcionais
        for extra in (extra_signers or []):
            signers.append(extra)

        # Monta form_fields no formato ZapSign
        zap_fields = []
        if form_fields:
            for var_name, value in form_fields.items():
                zap_fields.append({
                    "variable_name": var_name,
                    "value": str(value) if value is not None else "",
                })

        body = {
            "name":        doc_name,
            "lang":        "pt-br",
            "signers":     signers,
            "form_fields": zap_fields,
        }

        logger.info("ZapSign: criando documento '%s' com %d signatários", doc_name, len(signers))
        response = await self._post(f"/models/{template_token}/create-doc/", body)

        # Extrai URLs de assinatura
        doc_token     = response.get("token", "")
        open_id       = response.get("open_id", 0)
        resp_signers  = response.get("signers", [])

        lead_sign_url       = resp_signers[0].get("sign_url", "") if resp_signers else ""
        internal_sign_urls  = [s.get("sign_url", "") for s in resp_signers[1:]]

        logger.info(
            "ZapSign: documento criado token=%s open_id=%s lead_url=%s...",
            doc_token[:8], open_id, lead_sign_url[:40],
        )

        return {
            "doc_token":          doc_token,
            "open_id":            open_id,
            "lead_sign_url":      lead_sign_url,
            "internal_sign_urls": internal_sign_urls,
            "all_signers":        resp_signers,
        }

    # ------------------------------------------------------------------
    # Consulta de documento
    # ------------------------------------------------------------------

    async def get_document(self, doc_token: str) -> dict:
        """Retorna o status atual de um documento."""
        return await self._get(f"/docs/{doc_token}/")

    async def is_fully_signed(self, doc_token: str) -> bool:
        """Retorna True se todos os signatários já assinaram."""
        try:
            doc = await self.get_document(doc_token)
            status = doc.get("status", "")
            return status == "signed"
        except ZapSignError:
            return False


# ---------------------------------------------------------------------------
# Utilitário: resolve template pelo nome da administradora
# ---------------------------------------------------------------------------

def get_template_for_adm(adm: str) -> str | None:
    """
    Retorna o token do template ZapSign para a administradora informada.
    A busca é case-insensitive e parcial (ex.: 'Caixa Econômica' → template Caixa).
    Retorna None se não encontrar mapeamento.
    """
    adm_lower = (adm or "").lower()
    for key, token in TEMPLATE_BY_ADM.items():
        if key in adm_lower:
            return token
    return None


# ---------------------------------------------------------------------------
# Campos padrão do contrato mapeados do card FARO
# Ajuste os variable_name conforme o retorno de get_template()
# ---------------------------------------------------------------------------

def build_form_fields(card: dict) -> dict[str, str]:
    """
    Monta o dict de form_fields para o ZapSign a partir dos dados do card FARO.

    IMPORTANTE: os variable_name devem corresponder exatamente aos campos
    configurados em cada template. Use ZapSignClient.get_template() para
    inspecionar os nomes reais antes de colocar em produção.

    Campos disponíveis no card FARO:
        Nome do contato, CPF, Adm, Grupo, Cota, Crédito, Proposta Realizada,
        Endereço, Numero do endereço, Bairro, CEP, Cidade, Estado, Complemento,
        Estado Civil, Ocupação, Nacionalidade, Telefone, Email,
        Parcelas pagas, Parcelas a vencer, Valor das parcelas, Tipo de bem
    """
    def _fmt_currency(val) -> str:
        """Formata valor numérico como moeda brasileira."""
        try:
            return f"R$ {float(val):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            return str(val) if val else ""

    return {
        # Identificação do cliente
        "nome_completo":      card.get("Nome do contato") or card.get("title", ""),
        "cpf":                card.get("CPF", ""),
        "nacionalidade":      card.get("Nacionalidade", "Brasileira"),
        "estado_civil":       card.get("Estado Civil", ""),
        "ocupacao":           card.get("Ocupação", ""),
        "email":              card.get("Email", ""),
        "telefone":           card.get("Telefone", ""),

        # Endereço
        "cep":                card.get("CEP", ""),
        "endereco":           card.get("Endereço", ""),
        "numero":             card.get("Numero do endereço", ""),
        "complemento":        card.get("Complemento", ""),
        "bairro":             card.get("Bairro", ""),
        "cidade":             card.get("Cidade", ""),
        "estado":             card.get("Estado", ""),

        # Dados da cota
        "administradora":     card.get("Adm", ""),
        "grupo":              card.get("Grupo", ""),
        "cota":               card.get("Cota", ""),
        "tipo_de_bem":        card.get("Tipo de bem", ""),
        "credito":            _fmt_currency(card.get("Crédito")),
        "parcelas_pagas":     str(card.get("Parcelas pagas") or ""),
        "parcelas_a_vencer":  str(card.get("Parcelas a vencer") or ""),
        "valor_parcelas":     _fmt_currency(card.get("Valor das parcelas")),

        # Valores da negociação
        "valor_proposta":     _fmt_currency(card.get("Proposta Realizada")),
        "proposta_aceita":    card.get("Proposta Aceita", ""),
    }
