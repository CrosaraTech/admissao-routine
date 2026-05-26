"""criar-zip.py — empacota o local-pipeline pra deploy num servidor.

Exclui automaticamente:
  - .env real (secrets — NUNCA empacotar)
  - dados sensíveis em runtime (admissoes.xlsx, payloads/, ndjson logs)
  - caches Python (__pycache__, .pyc)
  - venv (.venv/) — deve ser recriado no destino
  - backups antigos

Inclui:
  - todo o código .py
  - briefing.md
  - JSONs de config/regras/lookups/departamentos
  - funcoes_cbo.xlsx (catálogo CBO)
  - Logo da Crosara
  - DEPLOY.md, install.bat, run-*.bat, verificar-setup.py
  - .env.example (template — vide ressalva no DEPLOY.md)

Uso:
    .\\.venv\\Scripts\\python.exe criar-zip.py
    ^^ ou:
    python criar-zip.py
"""
from __future__ import annotations

import sys
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).parent

# Diretórios INTEIROS a excluir (qualquer profundidade)
EXCLUDE_DIRS = {
    "__pycache__",
    "payloads",
    "backups",
    ".venv",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "venv",
    "env",
}

# Arquivos específicos a excluir
EXCLUDE_FILES = {
    ".env",                     # secrets reais
    "admissoes.xlsx",           # dados sensíveis (CPFs, nomes)
    "admissao_log.ndjson",      # idem
    "billing.ndjson",           # contém timestamps de uso
    "econtador_audit.ndjson",   # contém CNPJs/IDs
    ".DS_Store",
    "Thumbs.db",
    "criar-zip.py",             # não precisa no destino
}

# Extensões a excluir
EXCLUDE_EXT = {".pyc", ".pyo", ".log", ".swp", ".tmp"}


def should_exclude(p: Path) -> tuple[bool, str]:
    """Retorna (excluir, motivo) pra um path relativo a ROOT."""
    # Verifica diretórios na hierarquia
    for part in p.parts:
        if part in EXCLUDE_DIRS:
            return True, f"dir '{part}/'"
    # Arquivo específico
    if p.name in EXCLUDE_FILES:
        return True, "arquivo bloqueado"
    # Extensão
    if p.suffix.lower() in EXCLUDE_EXT:
        return True, f"ext '{p.suffix}'"
    return False, ""


def main() -> int:
    if not (ROOT / "interface.py").exists():
        print("[ERRO] não estou na pasta local-pipeline (interface.py ausente)")
        return 1

    ts = datetime.now().strftime("%Y%m%d-%H%M")
    zip_name = f"local-pipeline-{ts}.zip"
    zip_path = ROOT.parent / zip_name

    incluidos: list[Path] = []
    excluidos: dict[str, list[str]] = {}

    print()
    print("=" * 60)
    print(f"Empacotando local-pipeline em:")
    print(f"  {zip_path}")
    print("=" * 60)
    print()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(ROOT.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            excluir, motivo = should_exclude(rel)
            if excluir:
                excluidos.setdefault(motivo, []).append(str(rel))
                continue
            # Inclui mantendo prefixo "local-pipeline/" no zip (pra extrair direto)
            z.write(p, arcname=f"local-pipeline/{rel}")
            incluidos.append(rel)

    # Tamanho final
    size_mb = zip_path.stat().st_size / 1024 / 1024

    # Resumo (ASCII-safe pra cp1252 do Windows)
    print(f"  Incluidos: {len(incluidos)} arquivos")
    importantes = ["interface.py", "main.py", "DEPLOY.md", "install.bat",
                   ".env.example", "config.json", "briefing.md"]
    for nome in importantes:
        marcador = "[OK]" if any(str(p) == nome for p in incluidos) else "[FALTA]"
        print(f"     {marcador} {nome}")

    print()
    print("  Excluidos:")
    for motivo, lista in sorted(excluidos.items(), key=lambda x: -len(x[1])):
        print(f"     - {len(lista)} arquivo(s) por: {motivo}")
        for x in lista[:3]:
            print(f"          {x}")
        if len(lista) > 3:
            print(f"          ... +{len(lista) - 3} mais")

    print()
    print("=" * 60)
    print(f"OK! Arquivo: {zip_path}")
    print(f"     Tamanho: {size_mb:.1f} MB")
    print("=" * 60)
    print()
    print("Pra fazer deploy no servidor:")
    print(f"  1. Copie {zip_name} pro servidor (cópia, scp, etc.)")
    print("  2. Descompacte: Expand-Archive local-pipeline-*.zip -DestinationPath .")
    print("  3. cd local-pipeline")
    print("  4. install.bat")
    print("  5. copy .env.example .env  (editar com os tokens reais)")
    print("  6. run-verificar.bat  (valida tudo)")
    print("  7. run-gui.bat  ou  run-once.bat")

    return 0


if __name__ == "__main__":
    sys.exit(main())
