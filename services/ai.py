"""
services/ai.py — Cliente unificado de IA (OpenAI / Anthropic Claude / Google Gemini)
Centraliza todas as chamadas de IA do sistema num único ponto.
"""

import base64
import logging
import mimetypes
from typing import Literal, Optional

import httpx

from config import (
    OPENAI_API_KEY,
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    DEFAULT_AI_MODEL,
    DEFAULT_VISION_MODEL,
)

logger = logging.getLogger(__name__)

AIProvider = Literal["openai", "anthropic", "gemini"]

# Modelos de fallback por provider — usados quando o modelo primário falha
_VISION_FALLBACK_CHAIN = [
    # (modelo, aceita_pdf)
    ("gpt-4o",              False),
    ("gemini-2.5-flash",    True),
    ("claude-3-5-sonnet-20241022", False),
]


class AIError(Exception):
    pass


class AIClient:
    """
    Interface unificada para chamadas de IA.

    Detecta o provider pelo modelo:
      - gpt-*        → OpenAI
      - claude-*     → Anthropic
      - gemini-*     → Google Gemini

    Uso:
        ai = AIClient()
        resposta = await ai.complete(
            prompt="Gere uma mensagem de follow-up...",
            model="gpt-4o-mini",  # ou omita para usar DEFAULT_AI_MODEL
        )
    """

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=60.0)

    async def aclose(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    def _detect_provider(self, model: str) -> AIProvider:
        m = model.lower()
        if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3"):
            return "openai"
        if m.startswith("claude"):
            return "anthropic"
        if m.startswith("gemini"):
            return "gemini"
        raise AIError(f"Provider não reconhecido para o modelo '{model}'")

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------

    async def _openai(self, prompt: str, model: str, system: str, max_tokens: int) -> str:
        if not OPENAI_API_KEY:
            raise AIError("OPENAI_API_KEY não configurada")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        r = await self._client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    # ------------------------------------------------------------------
    # Anthropic Claude
    # ------------------------------------------------------------------

    async def _anthropic(self, prompt: str, model: str, system: str, max_tokens: int) -> str:
        if not ANTHROPIC_API_KEY:
            raise AIError("ANTHROPIC_API_KEY não configurada")
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system

        r = await self._client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json=body,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()

    # ------------------------------------------------------------------
    # Google Gemini
    # ------------------------------------------------------------------

    async def _gemini(self, prompt: str, model: str, system: str, max_tokens: int) -> str:
        if not GEMINI_API_KEY:
            raise AIError("GEMINI_API_KEY não configurada")
        contents = []
        if system:
            contents.append({"role": "user", "parts": [{"text": f"[System: {system}]\n\n{prompt}"}]})
        else:
            contents.append({"role": "user", "parts": [{"text": prompt}]})

        r = await self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": contents,
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    # ------------------------------------------------------------------
    # Interface pública
    # ------------------------------------------------------------------

    async def complete_with_history(
        self,
        history: list[dict],
        system: str = "",
        max_tokens: int = 500,
        model: str = None,
        fallback_model: str = None,
    ) -> str:
        """
        Gera resposta usando histórico de conversa.

        Args:
            history: Lista de dicts {"role": "user"|"assistant", "content": str}
            system:  Prompt de sistema.
            model:   Modelo primário (padrão: gemini-2.0-flash).
            fallback_model: Modelo de fallback (padrão: gpt-4o-mini).

        Tenta o modelo primário; em caso de falha, tenta o fallback.
        """
        primary  = model or "gemini-2.5-flash"
        fallback = fallback_model or "gpt-4o-mini"

        for attempt_model in (primary, fallback):
            try:
                provider = self._detect_provider(attempt_model)
                logger.info("AI history: provider=%s model=%s turns=%d", provider, attempt_model, len(history))

                if provider == "gemini":
                    return await self._gemini_chat(history, system, attempt_model, max_tokens)
                elif provider == "openai":
                    return await self._openai_chat(history, system, attempt_model, max_tokens)
                elif provider == "anthropic":
                    return await self._anthropic_chat(history, system, attempt_model, max_tokens)

            except (AIError, httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning("AI history: falha em %s (%s). Tentando fallback.", attempt_model, e)

        raise AIError(f"Todos os modelos falharam (primário={primary}, fallback={fallback})")

    async def _gemini_chat(self, history: list[dict], system: str, model: str, max_tokens: int) -> str:
        if not GEMINI_API_KEY:
            raise AIError("GEMINI_API_KEY não configurada")
        contents = []
        if system:
            contents.append({"role": "user", "parts": [{"text": f"[Instruções do sistema: {system}]"}]})
            contents.append({"role": "model", "parts": [{"text": "Entendido."}]})
        for msg in history:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        r = await self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={"contents": contents, "generationConfig": {"maxOutputTokens": max_tokens}},
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    async def _openai_chat(self, history: list[dict], system: str, model: str, max_tokens: int) -> str:
        if not OPENAI_API_KEY:
            raise AIError("OPENAI_API_KEY não configurada")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        r = await self._client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    async def _anthropic_chat(self, history: list[dict], system: str, model: str, max_tokens: int) -> str:
        if not ANTHROPIC_API_KEY:
            raise AIError("ANTHROPIC_API_KEY não configurada")
        body = {"model": model, "max_tokens": max_tokens, "messages": history}
        if system:
            body["system"] = system
        r = await self._client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json=body,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()

    async def complete(
        self,
        prompt: str,
        model: str = None,
        system: str = "",
        max_tokens: int = 500,
    ) -> str:
        """
        Gera uma resposta de texto.

        Args:
            prompt: O prompt do usuário.
            model: Modelo a usar (padrão: DEFAULT_AI_MODEL do .env).
            system: Instrução de sistema opcional.
            max_tokens: Máximo de tokens na resposta.

        Returns:
            Texto gerado pelo modelo.
        """
        model = model or DEFAULT_AI_MODEL
        provider = self._detect_provider(model)
        logger.info("AI complete: provider=%s model=%s tokens=%d", provider, model, max_tokens)

        try:
            if provider == "openai":
                return await self._openai(prompt, model, system, max_tokens)
            elif provider == "anthropic":
                return await self._anthropic(prompt, model, system, max_tokens)
            elif provider == "gemini":
                return await self._gemini(prompt, model, system, max_tokens)
        except httpx.HTTPStatusError as e:
            raise AIError(f"Erro HTTP {e.response.status_code} na IA: {e.response.text[:200]}") from e
        except httpx.RequestError as e:
            raise AIError(f"Erro de rede na IA: {e}") from e

    # ------------------------------------------------------------------
    # Análise de imagem / documento (visão)
    # ------------------------------------------------------------------

    async def _download_media(self, url: str) -> tuple[bytes, str]:
        """
        Faz download de uma mídia por URL e retorna (bytes, mime_type).
        O mime_type é detectado pelo Content-Type da resposta.
        """
        try:
            r = await self._client.get(url, follow_redirects=True)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
            return r.content, content_type
        except httpx.RequestError as e:
            raise AIError(f"Erro ao baixar mídia: {e}") from e
        except httpx.HTTPStatusError as e:
            raise AIError(f"HTTP {e.response.status_code} ao baixar mídia") from e

    async def _openai_vision(
        self, prompt: str, image_b64: str, mime_type: str, system: str, max_tokens: int, model: str
    ) -> str:
        if not OPENAI_API_KEY:
            raise AIError("OPENAI_API_KEY não configurada")
        # OpenAI não suporta PDF — rejeita explicitamente para acionar fallback
        if mime_type == "application/pdf":
            raise AIError("OpenAI vision não suporta PDF — use Gemini")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image_b64}", "detail": "high"},
                },
                {"type": "text", "text": prompt},
            ],
        })
        r = await self._client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    async def _anthropic_vision(
        self, prompt: str, image_b64: str, mime_type: str, system: str, max_tokens: int, model: str
    ) -> str:
        if not ANTHROPIC_API_KEY:
            raise AIError("ANTHROPIC_API_KEY não configurada")
        # Claude suporta image/jpeg, image/png, image/gif, image/webp — não PDF
        if mime_type == "application/pdf":
            raise AIError("Anthropic vision não suporta PDF — use Gemini")
        supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        if mime_type not in supported:
            mime_type = "image/jpeg"  # fallback conservador

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        }
        if system:
            body["system"] = system
        r = await self._client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json=body,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()

    async def _gemini_vision(
        self, prompt: str, image_b64: str, mime_type: str, system: str, max_tokens: int, model: str
    ) -> str:
        """Gemini suporta imagens e PDFs nativamente via inline data."""
        if not GEMINI_API_KEY:
            raise AIError("GEMINI_API_KEY não configurada")
        text_intro = f"[System: {system}]\n\n{prompt}" if system else prompt
        contents = [{
            "role": "user",
            "parts": [
                {"inlineData": {"mimeType": mime_type, "data": image_b64}},
                {"text": text_intro},
            ],
        }]
        r = await self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": contents,
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    async def complete_with_image(
        self,
        prompt: str,
        media_url: str,
        system: str = "",
        max_tokens: int = 600,
        model: Optional[str] = None,
    ) -> str:
        """
        Analisa uma imagem ou documento usando um modelo com capacidade de visão.

        Baixa a mídia da URL fornecida, converte para base64 e envia junto com o
        prompt para o modelo de visão configurado.

        Suporte:
          - OpenAI GPT-4o: imagens (JPEG, PNG, WEBP, GIF) — NÃO suporta PDF
          - Anthropic Claude 3+: imagens (JPEG, PNG, WEBP, GIF) — NÃO suporta PDF
          - Google Gemini: imagens E PDFs nativamente

        Para PDFs, sempre usa Gemini independente do DEFAULT_VISION_MODEL.
        Em caso de falha do modelo primário, tenta a cadeia de fallback.

        Args:
            prompt:    Instrução ao modelo sobre o que extrair/analisar.
            media_url: URL do documento ou imagem (e.g., URL temporária do Whapi).
            system:    Prompt de sistema opcional.
            max_tokens: Máximo de tokens na resposta.
            model:     Modelo a usar (padrão: DEFAULT_VISION_MODEL do .env).

        Returns:
            Texto gerado pelo modelo.
        """
        # Baixa a mídia primeiro (faz uma só vez, reutiliza nos fallbacks)
        media_bytes, mime_type = await self._download_media(media_url)
        image_b64 = base64.b64encode(media_bytes).decode("utf-8")
        is_pdf = mime_type == "application/pdf"

        logger.info(
            "AI vision: baixou %d bytes, mime=%s, is_pdf=%s",
            len(media_bytes), mime_type, is_pdf,
        )

        # Se for PDF, força Gemini como primeiro da fila
        preferred = model or DEFAULT_VISION_MODEL
        if is_pdf and not preferred.startswith("gemini"):
            logger.info("AI vision: PDF detectado — substituindo %s por gemini-2.5-flash", preferred)
            preferred = "gemini-2.5-flash"

        # Monta cadeia: modelo preferido + fallbacks que suportam o mime
        candidates: list[str] = [preferred]
        for fallback_model, accepts_pdf in _VISION_FALLBACK_CHAIN:
            if fallback_model != preferred:
                if is_pdf and not accepts_pdf:
                    continue  # pula modelos que não aceitam PDF
                candidates.append(fallback_model)

        last_error: Exception = AIError("Sem modelos disponíveis")
        for attempt_model in candidates:
            provider = self._detect_provider(attempt_model)
            logger.info("AI vision: tentando provider=%s model=%s", provider, attempt_model)
            try:
                if provider == "openai":
                    result = await self._openai_vision(prompt, image_b64, mime_type, system, max_tokens, attempt_model)
                elif provider == "anthropic":
                    result = await self._anthropic_vision(prompt, image_b64, mime_type, system, max_tokens, attempt_model)
                elif provider == "gemini":
                    result = await self._gemini_vision(prompt, image_b64, mime_type, system, max_tokens, attempt_model)
                else:
                    continue
                logger.info("AI vision: sucesso com %s", attempt_model)
                return result
            except (AIError, httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning("AI vision: falha em %s (%s), tentando próximo.", attempt_model, e)
                last_error = e

        raise AIError(
            f"Todos os modelos de visão falharam para mime={mime_type}. "
            f"Último erro: {last_error}"
        )

    async def format_phone(self, raw_phone: str) -> str:
        """
        Usa IA para normalizar um número de telefone para o formato 5511999999999.
        Fallback: limpeza manual sem IA.
        """
        # Tenta limpeza direta primeiro (mais rápido e barato)
        digits = "".join(c for c in raw_phone if c.isdigit())
        if digits.startswith("55") and len(digits) in (12, 13):
            return digits
        if not digits.startswith("55"):
            digits = "55" + digits
        if len(digits) in (12, 13):
            return digits

        # Se não resolveu, usa IA
        try:
            result = await self.complete(
                prompt=(
                    f"Formate o valor '{raw_phone}' para o padrão internacional 5511999999999.\n"
                    "Regras: Remova '+', parênteses, espaços e hifens. Certifique-se de que o resultado "
                    "contenha apenas números e comece com 55.\n"
                    "Saída: Retorne apenas o número corrigido, sem texto adicional."
                ),
                max_tokens=20,
            )
            cleaned = "".join(c for c in result if c.isdigit())
            return cleaned if cleaned else digits
        except AIError:
            return digits

    async def generate_followup(self, hour: int = 12) -> str:
        """
        Gera mensagem de follow-up personalizada pelo horário.
        Baseado no prompt original do blueprint Follow up.
        """
        saudacao = "Bom dia" if hour < 12 else ("Boa tarde" if hour < 18 else "Boa noite")
        return await self.complete(
            prompt=(
                f"Gere uma mensagem de follow-up com o intuito de perguntar se o cliente analisou a proposta. "
                f"Mantenha um tom profissional. Exemplo: 'Olá! Espero que esteja bem. Já teve a oportunidade "
                f"de analisar a proposta? Caso tenha qualquer dúvida, estou à disposição. Obrigado!' "
                f"(não utilize o nome do cliente). Use '{saudacao}' como saudação inicial."
            ),
            max_tokens=150,
        )
