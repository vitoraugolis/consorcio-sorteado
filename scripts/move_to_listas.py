"""
Script utilitário — move o card de teste de volta para o stage LISTAS.
Uso: python scripts/move_to_listas.py
"""
import asyncio
import sys
import os

# Adiciona o diretório raiz ao path para importar os módulos do projeto
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import TEST_PHONE, Stage
from services.faro import FaroClient, get_phone


async def main():
    phone_digits = "".join(c for c in TEST_PHONE if c.isdigit())
    print(f"Buscando card com telefone: {TEST_PHONE}")

    async with FaroClient() as faro:
        # Tenta encontrar o card pelo telefone
        card = await faro.find_card_by_phone(phone_digits)

        if not card:
            print("❌ Card não encontrado via find_card_by_phone. Tentando busca por stage...")
            # Busca em todos os stages relevantes
            stages_to_search = [
                ("LISTAS",           Stage.LISTAS),
                ("BAZAR",            Stage.BAZAR),
                ("PRIMEIRA_ATIVACAO", Stage.PRIMEIRA_ATIVACAO),
                ("SEGUNDA_ATIVACAO",  Stage.SEGUNDA_ATIVACAO),
                ("TERCEIRA_ATIVACAO", Stage.TERCEIRA_ATIVACAO),
                ("QUARTA_ATIVACAO",   Stage.QUARTA_ATIVACAO),
                ("PRECIFICACAO",      Stage.PRECIFICACAO),
                ("EM_NEGOCIACAO",     Stage.EM_NEGOCIACAO),
                ("ACEITO",            Stage.ACEITO),
                ("ASSINATURA",        Stage.ASSINATURA),
                ("NEG_CONGELADA",     Stage.NEG_CONGELADA),
                ("ON_HOLD",           Stage.ON_HOLD),
                ("TESTES",            Stage.TESTES),
                ("FLUXO_CADENCIA",    Stage.FLUXO_CADENCIA),
            ]
            for stage_name, stage_id in stages_to_search:
                cards = await faro.get_cards_from_stage(stage_id=stage_id, limit=100)
                for c in cards:
                    phone = get_phone(c)
                    if phone and phone_digits in phone:
                        card = c
                        print(f"✅ Card encontrado em stage: {stage_name}")
                        break
                if card:
                    break

        if not card:
            print(f"❌ Card com telefone {TEST_PHONE} não encontrado em nenhum stage.")
            return

        card_id   = card.get("id", "")
        nome      = card.get("Nome do contato") or card.get("title") or "?"
        stage_atual = card.get("stage_id") or card.get("stageId") or "?"

        print(f"\nCard encontrado:")
        print(f"  ID:     {card_id}")
        print(f"  Nome:   {nome}")
        print(f"  Stage:  {stage_atual}")

        if stage_atual == Stage.LISTAS:
            print("\n✅ Card já está em LISTAS. Nenhuma ação necessária.")
            return

        # Reseta campos de ativação para um início limpo
        reset_fields = {
            "Num Ativacoes":   "0",
            "Num Follow Ups":  "0",
            "Recusas":         "0",
            "Ultima atividade": "",
            "Data proposta enviada": "",
            "Aguardando Extrato": "",
            "ZapSign Token": "",
        }
        print("\nResetando campos de ativação...")
        await faro.update_card(card_id, reset_fields)

        print(f"Movendo para LISTAS ({Stage.LISTAS})...")
        await faro.move_card(card_id, Stage.LISTAS)

        print(f"\n✅ Card '{nome}' movido para LISTAS com sucesso!")


if __name__ == "__main__":
    asyncio.run(main())
