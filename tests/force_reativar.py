"""
Força reativação de um card específico pelo telefone, ignorando timing do FARO.
Uso: python3 tests/force_reativar.py <stage>
  stage: primeira | segunda | terceira | quarta
"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from config import Stage, TEST_PHONE
from services.faro import FaroClient, get_phone
from jobs.reativador import _process_card

STAGE_MAP = {
    "primeira": Stage.PRIMEIRA_ATIVACAO,
    "segunda":  Stage.SEGUNDA_ATIVACAO,
    "terceira": Stage.TERCEIRA_ATIVACAO,
    "quarta":   Stage.QUARTA_ATIVACAO,
}

async def main():
    stage_name = sys.argv[1] if len(sys.argv) > 1 else "primeira"
    stage_id = STAGE_MAP.get(stage_name)
    if not stage_id:
        print(f"Stage inválido: {stage_name}. Use: {list(STAGE_MAP)}")
        return

    phone = TEST_PHONE
    print(f"Buscando card para {phone} em stage={stage_name}...")

    async with FaroClient() as faro:
        card = await faro.find_card_by_phone(phone)

    if not card:
        print(f"Card não encontrado para {phone}")
        return

    print(f"Card encontrado: {card.get('title') or card.get('Nome do contato')} | stage atual: {card.get('stage_id','?')[:8]}")
    print(f"Disparando reativação [{stage_name}]...")

    ok = await _process_card(card, stage_id)
    print("✅ OK" if ok else "❌ Falhou")

asyncio.run(main())
