"""Cliente da API E-plugin Alterdata (eContador)."""

from __future__ import annotations

import logging
import re

import httpx


log = logging.getLogger("admissao.ecotador")


def cnpj_limpo(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


class EContadorAPI:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/vnd.api+json",
                "Accept": "application/vnd.api+json",
            },
            timeout=60.0,
        )

    def close(self) -> None:
        self.client.close()

    # ---- Empresa -------------------------------------------------

    def resolver_empresa(self, cnpj: str) -> tuple[str | None, dict]:
        """Retorna (empresa_id, attributes) ou (None, {})."""
        cnpj_d = cnpj_limpo(cnpj)
        if not cnpj_d:
            return None, {}
        r = self.client.get(
            f"{self.base}/empresas",
            params={"filter[cpfcnpj]": cnpj_d, "page[limit]": 5},
        )
        if r.status_code != 200:
            log.error(f"GET /empresas cnpj={cnpj_d}: HTTP {r.status_code} — {r.text[:200]}")
            return None, {}
        data = r.json().get("data", [])
        if not data:
            return None, {}
        return str(data[0]["id"]), data[0].get("attributes", {})

    # ---- Departamento -------------------------------------------

    def listar_departamentos(self, empresa_id: str) -> list[dict]:
        """Retorna lista de {id, nome} dos departamentos da empresa."""
        r = self.client.get(
            f"{self.base}/departamentos",
            params={"filter[empresaId]": empresa_id, "page[limit]": 200},
        )
        if r.status_code != 200:
            log.error(
                f"GET /departamentos empresaId={empresa_id}: "
                f"HTTP {r.status_code} — {r.text[:200]}"
            )
            return []
        return [
            {"id": str(it["id"]), "nome": (it.get("attributes") or {}).get("nome", "?")}
            for it in r.json().get("data", [])
        ]

    # ---- Candidato ----------------------------------------------

    def post_candidato(self, payload: dict) -> tuple[bool, str, str]:
        """Retorna (ok, candidato_id_ou_erro, body_erro)."""
        r = self.client.post(f"{self.base}/candidatos", json=payload)
        if r.status_code == 201:
            return True, str(r.json()["data"]["id"]), ""
        return False, f"HTTP {r.status_code}", r.text[:2000]
