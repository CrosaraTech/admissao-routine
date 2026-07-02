"""Utilitarios pra CNPJ — validacao de digito verificador.

Usado pra rejeitar typos de OCR antes de chamar GET /empresas em vao
e pra validar input do usuario no botao 'Corrigir CNPJ' da UI.
"""
from __future__ import annotations


def so_digitos(cnpj: str) -> str:
    """Remove qualquer caractere nao-digito de uma string de CNPJ."""
    return "".join(c for c in str(cnpj or "") if c.isdigit())


def validar_cnpj(cnpj: str) -> bool:
    """Valida CNPJ pelo algoritmo de digito verificador.

    Aceita com ou sem formatacao (pontos/barras/traco).
    Retorna False pra strings com tamanho != 14, todos digitos iguais (00..0,
    11..1, etc.), ou DV invalido.
    """
    d = so_digitos(cnpj)
    if len(d) != 14:
        return False
    if len(set(d)) == 1:  # 00000000000000, 11111111111111, etc.
        return False

    nums = [int(c) for c in d]

    def calc_dv(seq: list[int], pesos: list[int]) -> int:
        s = sum(n * p for n, p in zip(seq, pesos))
        r = s % 11
        return 0 if r < 2 else 11 - r

    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]

    dv1 = calc_dv(nums[:12], pesos1)
    dv2 = calc_dv(nums[:13], pesos2)
    return nums[12] == dv1 and nums[13] == dv2


def formatar_cnpj(cnpj: str) -> str:
    """Formata CNPJ pra XX.XXX.XXX/XXXX-XX. Retorna entrada se nao tem 14 digitos."""
    d = so_digitos(cnpj)
    if len(d) != 14:
        return cnpj
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
