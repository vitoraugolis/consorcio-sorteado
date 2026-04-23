"""
services/html_image.py — Renderiza HTML para imagem PNG usando Playwright

Playwright usa o Chromium headless embutido: gradientes, clip-path, CSS grid,
web fonts — tudo funciona igual a um browser real, sem dependência externa.

Uso:
    from services.html_image import render_to_file
    path = await render_to_file(html_string, "proposta_abc123.png")
    # path → "/tmp/cs_images/proposta_abc123.png"
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Diretório onde os PNGs são salvos — compartilhado com o StaticFiles do FastAPI
IMAGES_DIR = Path(os.getenv("IMAGES_DIR", "/tmp/cs_images"))


async def render_to_file(html: str, filename: str, width: int = 800) -> str | None:
    """
    Renderiza uma string HTML para um arquivo PNG.
    Retorna o caminho absoluto do arquivo ou None em caso de falha.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "Playwright não instalado — imagem da proposta desativada. "
            "Instale com: pip install playwright && playwright install chromium"
        )
        return None

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    output_path = IMAGES_DIR / filename

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.set_content(html, wait_until="networkidle")
            await page.screenshot(path=str(output_path), full_page=True)
            await browser.close()

        logger.info("Imagem gerada: %s (%d bytes)", output_path, output_path.stat().st_size)
        return str(output_path)

    except Exception as e:
        logger.error("Falha ao renderizar HTML para PNG: %s", e)
        return None
