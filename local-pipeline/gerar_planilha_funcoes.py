"""Gera/atualiza a planilha funcoes_cbo.xlsx puxando TODAS as funções do eContador.

Uso:
    python gerar_planilha_funcoes.py [--out funcoes_cbo.xlsx]

Lê ECONTADOR_TOKEN do .env e pagina /funcoes (cap=200/página, ~9k itens).
Gera planilha com colunas: funcao_id, nome_cargo, cbo, codigo, externoid.

Reaproveitável: rode periodicamente pra manter a planilha sincronizada
com cadastros novos no eContador.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import httpx
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gerar-planilha")


BASE_URL = "https://dp.pack.alterdata.com.br/api/v1"
PAGE_LIMIT = 200  # Cap real da API; > 200 retorna HTTP 500


def fetch_todas_funcoes(token: str) -> list[dict]:
    """Pagina todas as funções e retorna lista de attributes + id."""
    funcoes: list[dict] = []
    offset = 0
    with httpx.Client(
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.api+json",
        },
        timeout=60.0,
    ) as client:
        # Primeira chamada pra pegar total
        r = client.get(
            f"{BASE_URL}/funcoes",
            params={"page[limit]": PAGE_LIMIT, "page[offset]": 0},
        )
        r.raise_for_status()
        body = r.json()
        total = body.get("meta", {}).get("totalResourceCount")
        log.info(f"Total reportado pelo servidor: {total}")

        while True:
            if offset > 0:
                r = client.get(
                    f"{BASE_URL}/funcoes",
                    params={"page[limit]": PAGE_LIMIT, "page[offset]": offset},
                )
                r.raise_for_status()
                body = r.json()

            data = body.get("data", [])
            if not data:
                break

            for it in data:
                a = it.get("attributes") or {}
                funcoes.append({
                    "funcao_id": str(it.get("id", "")),
                    "nome_cargo": a.get("nome", ""),
                    "cbo": a.get("cbo", ""),
                    "codigo": a.get("codigo", ""),
                    "externoid": a.get("externoid", ""),
                })

            log.info(f"  offset={offset} → +{len(data)} (total acumulado: {len(funcoes)})")
            if len(data) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
            # Pequena pausa pra não martelar o servidor
            time.sleep(0.1)

    return funcoes


def gravar_xlsx(funcoes: list[dict], path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "funcoes"

    headers = ["funcao_id", "nome_cargo", "cbo", "codigo", "externoid"]
    ws.append(headers)

    # Estilo do cabeçalho
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = header_font
        cell.fill = header_fill

    for f in funcoes:
        ws.append([f.get(h, "") for h in headers])

    # Ajuste de largura das colunas
    larguras = {"A": 12, "B": 60, "C": 10, "D": 12, "E": 38}
    for col, w in larguras.items():
        ws.column_dimensions[col].width = w

    # Freeze cabeçalho
    ws.freeze_panes = "A2"

    wb.save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera funcoes_cbo.xlsx do eContador")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent / "funcoes_cbo.xlsx"),
        help="Caminho de saída (default: ./funcoes_cbo.xlsx)",
    )
    args = parser.parse_args()

    token = os.getenv("ECONTADOR_TOKEN")
    if not token:
        log.error("ECONTADOR_TOKEN não encontrado no ambiente (.env)")
        return 1

    log.info("Puxando todas as funções do eContador...")
    funcoes = fetch_todas_funcoes(token)
    log.info(f"✅ {len(funcoes)} funções coletadas")

    out_path = Path(args.out)
    gravar_xlsx(funcoes, out_path)
    log.info(f"📊 Planilha salva em {out_path} ({out_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
