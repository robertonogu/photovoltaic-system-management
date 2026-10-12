"""Leitura das faturas da MEO Energia (PDF).

Só tira os valores de faturação: período, kWh, preços, taxas, IVA e total.
Não guarda nome, morada, NIF nem IBAN.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime

NUM = r"\d{1,3}(?: \d{3})*,\d{2,4}"  # 1 340,00 · 0,1499 · 20,21


class FaturaIlegivel(Exception):
    pass


def _n(txt: str) -> float:
    return float(txt.replace(" ", "").replace(".", "").replace(",", "."))


def _d(txt: str) -> str:
    return datetime.strptime(txt, "%d/%m/%Y").date().isoformat()


def texto_do_pdf(dados: bytes, senha: str | None = None) -> str:
    from pypdf import PdfReader
    try:
        r = PdfReader(io.BytesIO(dados))
        if r.is_encrypted:
            if not senha or not r.decrypt(senha):
                raise FaturaIlegivel("O PDF tem palavra-passe. Indica-a em Definições (normalmente é o NIF).")
        return "\n".join((p.extract_text() or "") for p in r.pages)
    except FaturaIlegivel:
        raise
    except Exception as e:  # noqa: BLE001
        raise FaturaIlegivel(f"Não consegui abrir o PDF: {e}") from e


# linha de detalhe: "<descrição> 23% €  0,4700 43,00 (Dias) €  20,2109/07/2026 20/08/2026"
LINHA = re.compile(
    r"(?P<desc>[^\n]*?)\s*(?P<iva>\d{1,2})%\s*€\s*(?P<unit>" + NUM + r")\s+(?P<qtd>" + NUM + r")\s*\((?P<uni>Dias|kWh|UN)\)"
    r"\s*€\s*(?P<valor>-?" + r"\d{1,3}(?: \d{3})*,\d{2}" + r")\s*(?P<ini>\d{2}/\d{2}/\d{4})\s+(?P<fim>\d{2}/\d{2}/\d{4})")


def ler(texto: str) -> dict:
    if "MEO Energia" not in texto:
        raise FaturaIlegivel("Não parece uma fatura da MEO Energia.")
    f: dict = {"comercializador": "MEO Energia"}

    m = re.search(r"Fatura:\s*(.+?)\s+de\s+(\d{2}/\d{2}/\d{4})", texto)
    if not m:
        raise FaturaIlegivel("Não encontrei o número da fatura.")
    f["numero"] = re.sub(r"\s+", " ", m.group(1)).strip()
    f["emitida"] = _d(m.group(2))

    m = re.search(r"Período de faturação:\s*(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})", texto)
    if not m:
        raise FaturaIlegivel("Não encontrei o período de faturação.")
    f["inicio"], f["fim"] = _d(m.group(1)), _d(m.group(2))
    f["dias"] = (date.fromisoformat(f["fim"]) - date.fromisoformat(f["inicio"])).days + 1

    m = re.search(r"(" + NUM + r")\s*kVA", texto)
    f["potencia_kva"] = _n(m.group(1)) if m else None

    pot_dias = pot_valor = kwh = energia = cav = iec = dgeg = outros = 0.0
    precos_kwh, precos_pot = [], []
    linhas = 0
    for l in LINHA.finditer(texto):
        desc, valor = l["desc"].lower(), _n(l["valor"])
        qtd, unit = _n(l["qtd"]), _n(l["unit"])
        linhas += 1
        if "potência contratada" in desc or "potencia contratada" in desc:
            pot_dias += qtd
            pot_valor += valor
            precos_pot.append(unit)
        elif "imposto especial" in desc or "iec" in desc.split():
            iec += valor
        elif "audiovisual" in desc:
            cav += valor
        elif "consumo" in desc and l["uni"] == "kWh":
            kwh += qtd
            energia += valor
            precos_kwh.append(unit)
        elif "dgeg" in desc:
            dgeg += valor
        else:
            outros += valor
    if not linhas or not kwh and not pot_valor:
        raise FaturaIlegivel("Não encontrei as linhas de consumo da fatura.")

    m = re.search(r"€\s*(" + NUM + r")\s*TOTAL|TOTAL\s*€\s*(" + NUM + r")", texto)
    if not m:
        raise FaturaIlegivel("Não encontrei o total da fatura.")
    total = _n(m.group(1) or m.group(2))

    f.update({
        "kwh": round(kwh, 2),
        "preco_kwh": (precos_kwh[0] if len(set(precos_kwh)) == 1 else round(energia / kwh, 4)) if kwh else None,
        "potencia_dias": pot_dias,
        "potencia_eur_dia": (precos_pot[0] if len(set(precos_pot)) == 1 else round(pot_valor / pot_dias, 4)) if pot_dias else None,
        "energia_eur": round(energia, 2),
        "potencia_eur": round(pot_valor, 2),
        "cav_eur": round(cav, 2),
        "iec_eur": round(iec, 2),
        "dgeg_eur": round(dgeg, 2),
        "outros_eur": round(outros, 2),
        "total_eur": total,
    })
    f["iva_eur"] = round(total - (energia + pot_valor + cav + iec + dgeg + outros), 2)
    f["mudou_preco"] = len(set(precos_kwh)) > 1 or len(set(precos_pot)) > 1  # preço mudou a meio do período
    f["iec_kwh"] = round(iec / kwh, 4) if kwh and iec else 0.001

    m = re.search(r"encargo suplementar de\s*€\s*(" + NUM + r")", texto)
    f["extra_vs_regulado_eur"] = _n(m.group(1)) if m else None

    # leituras do contador (Cheias, Ponta, Vazio): tipo de leitura e quanto foi em vazio
    leit = re.findall(r"(" + NUM + r") kWh\s*(" + NUM + r") kWh\s*(" + NUM + r") kWh\s*([A-Za-zçãé]+)\s*(\d{2}/\d{2}/\d{4})", texto)
    f["leitura"] = "estimada" if any(t.lower().startswith("estim") for *_, t, _ in leit) else ("real" if leit else None)
    f["kwh_vazio"] = None
    if len(leit) >= 2:
        vazio = _n(leit[-1][2]) - _n(leit[0][2])
        total_reg = sum(_n(leit[-1][i]) - _n(leit[0][i]) for i in range(3))
        if 0 <= vazio <= total_reg:
            f["kwh_vazio"] = round(vazio, 2)

    # confirmação: o IVA que sobra tem de bater com as regras (6% na energia até ao limite e na CAV, 23% no resto)
    lim = 200 * f["dias"] / 30
    reduzida = energia * min(1.0, lim / kwh) if kwh else 0.0
    esperado = 0.06 * (reduzida + cav) + 0.23 * (energia - reduzida + pot_valor + iec + dgeg + outros)
    f["confere"] = abs(esperado - f["iva_eur"]) <= 0.05
    return f


def ler_pdf(dados: bytes, senha: str | None = None) -> dict:
    return ler(texto_do_pdf(dados, senha))
