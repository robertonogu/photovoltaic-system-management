"""Cálculo da poupança. Funções puras, sem base de dados, para serem fáceis de testar."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date


@dataclass
class Contrato:
    tipo: str  # "compra" ou "venda"
    empresa: str
    desde: str  # AAAA-MM-DD
    preco_kwh: float
    potencia_eur_dia: float = 0.0
    iva_potencia: float | None = None


@dataclass
class Impostos:
    iva_reduzido: float = 0.06
    iva_normal: float = 0.23
    limite_kwh_30dias: float = 200.0


def somar_meses(mes: str, n: int) -> str:
    a, m = map(int, mes.split("-"))
    t = a * 12 + (m - 1) + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def em_vigor(contratos: list[Contrato], tipo: str, mes: str) -> Contrato | None:
    """O contrato mais recente desse tipo que já tinha começado nesse mês."""
    validos = sorted((c for c in contratos if c.tipo == tipo and c.desde[:7] <= mes), key=lambda c: c.desde)
    return validos[-1] if validos else None


def custo_energia(kwh: float, preco: float, dias: int, imp: Impostos) -> float:
    """Energia com IVA: taxa reduzida nos primeiros kWh (por 30 dias), normal no resto."""
    limite = imp.limite_kwh_30dias * dias / 30
    return preco * (min(kwh, limite) * (1 + imp.iva_reduzido) + max(kwh - limite, 0) * (1 + imp.iva_normal))


def calcular(meses: list[dict], contratos: list[Contrato], faturas: dict[str, float],
             imp: Impostos, hoje: date | None = None) -> list[dict]:
    """meses: [{mes, consumo_kwh, rede_kwh, injetado_kwh, bateria_kwh}] ordenados ou não."""
    hoje = hoje or date.today()
    mes_atual = f"{hoje.year:04d}-{hoje.month:02d}"
    sem_contrato = Contrato("compra", "—", "0000-01-01", 0.0)
    acumulado = 0.0
    out = []
    for m in sorted(meses, key=lambda x: x["mes"]):
        mes = m["mes"]
        ano, mm = map(int, mes.split("-"))
        em_curso = mes == mes_atual
        dias = hoje.day if em_curso else calendar.monthrange(ano, mm)[1]
        compra = em_vigor(contratos, "compra", mes) or sem_contrato
        venda = em_vigor(contratos, "venda", mes)

        consumo = float(m.get("consumo_kwh") or 0)
        rede = float(m.get("rede_kwh") or 0)
        injetado = float(m.get("injetado_kwh") or 0)
        bateria = float(m.get("bateria_kwh") or 0)
        direto = max(consumo - rede - bateria, 0)

        iva_pot = imp.iva_normal if compra.iva_potencia is None else compra.iva_potencia
        potencia = compra.potencia_eur_dia * dias * (1 + iva_pot)
        com = custo_energia(rede, compra.preco_kwh, dias, imp) + potencia
        sem = custo_energia(consumo, compra.preco_kwh, dias, imp) + potencia
        venda_eur = injetado * (venda.preco_kwh if venda else 0)
        poupanca = sem - com + venda_eur
        acumulado += poupanca
        real = faturas.get(mes)
        out.append({
            "mes": mes, "em_curso": em_curso, "dias": dias,
            "direto_kwh": round(direto, 2), "bateria_kwh": round(bateria, 2), "rede_kwh": round(rede, 2),
            "injetado_kwh": round(injetado, 2), "consumo_kwh": round(consumo, 2),
            "solar_kwh": round(direto + bateria, 2),
            "fatura_real": real,
            "fatura_com": round(real if real is not None else com, 2),
            "fatura_sem": round(real + sem - com if real is not None else sem, 2),
            "venda_eur": round(venda_eur, 2),
            "evitado_eur": round(sem - com, 2),
            "poupanca_eur": round(poupanca, 2),
            "acumulado_eur": round(acumulado, 2),
            "empresa_compra": compra.empresa, "preco_kwh": compra.preco_kwh,
            "empresa_venda": venda.empresa if venda else "—",
        })
    return out


def resumo(resultados: list[dict], investimento: float) -> dict | None:
    if not resultados:
        return None
    fechados = [r for r in resultados if not r["em_curso"]]
    total = resultados[-1]["acumulado_eur"]
    soma_fechados = sum(r["poupanca_eur"] for r in fechados)
    media = soma_fechados / len(fechados) if fechados else 0
    falta = max(investimento - total, 0)
    previsao, meses_faltam = None, None
    if falta == 0:
        previsao, meses_faltam = "pago", 0
    elif media > 0:
        meses_faltam = int(-(-(investimento - soma_fechados) // media))  # arredonda para cima
        previsao = somar_meses(fechados[-1]["mes"], meses_faltam)
    solar = sum(r["solar_kwh"] for r in fechados)
    consumo = sum(r["consumo_kwh"] for r in fechados)
    ultimo = fechados[-1] if fechados else None
    return {
        "total": round(total, 2), "investimento": investimento,
        "recuperado": total / investimento if investimento else 0,
        "falta": round(falta, 2), "meses": len(fechados), "media": round(media, 2),
        "previsao": previsao, "meses_faltam": meses_faltam,
        "inicio": resultados[0]["mes"],
        "autossuficiencia": solar / consumo if consumo else None,
        "ultimo_mes": ultimo["mes"] if ultimo else None,
        "ultima_poupanca": ultimo["poupanca_eur"] if ultimo else None,
    }


# ---------------------------------------------------------------------------
# Faturas por período (as faturas não coincidem com os meses do calendário)
# ---------------------------------------------------------------------------
def _datas(inicio: str, fim: str):
    from datetime import timedelta
    d, f = date.fromisoformat(inicio), date.fromisoformat(fim)
    while d <= f:
        yield d
        d += timedelta(days=1)


def faturas_por_mes(faturas: list[dict]) -> dict[str, float]:
    """Reparte o total de cada fatura pelos meses, proporcionalmente aos dias.

    Só devolve os meses que ficam cobertos por faturas em todos os dias; os outros continuam estimados.
    """
    valor: dict[str, float] = {}
    cobertos: dict[str, set] = {}
    for f in faturas:
        dias = f["dias"] or 1
        for d in _datas(f["inicio"], f["fim"]):
            mes = d.strftime("%Y-%m")
            valor[mes] = valor.get(mes, 0.0) + f["total_eur"] / dias
            cobertos.setdefault(mes, set()).add(d.day)
    out = {}
    for mes, v in valor.items():
        a, m = map(int, mes.split("-"))
        if len(cobertos[mes]) == calendar.monthrange(a, m)[1]:
            out[mes] = round(v, 2)
    return out


def energia_no_periodo(inicio: str, fim: str, dias: dict[str, dict], meses: list[dict]) -> dict:
    """Consumo da casa, rede e injetado num período: pelos dados diários da Deye, ou pelos meses se faltarem dias."""
    datas = list(_datas(inicio, fim))
    com_dados = [dias[d.isoformat()] for d in datas if d.isoformat() in dias]
    if datas and len(com_dados) >= 0.9 * len(datas):
        fator = len(datas) / len(com_dados)  # completa os poucos dias em falta pela média
        soma = lambda k: sum((x.get(k) or 0) for x in com_dados) * fator  # noqa: E731
        return {"consumo_kwh": soma("consumo_kwh"), "rede_kwh": soma("rede_kwh"),
                "injetado_kwh": soma("injetado_kwh"), "fonte": "diária"}
    por_mes = {m["mes"]: m for m in meses}
    tot = {"consumo_kwh": 0.0, "rede_kwh": 0.0, "injetado_kwh": 0.0}
    faltam = 0
    for d in datas:
        m = por_mes.get(d.strftime("%Y-%m"))
        if not m:
            faltam += 1
            continue
        n = calendar.monthrange(d.year, d.month)[1]
        for k in tot:
            tot[k] += (m.get(k) or 0) / n
    if faltam > 0.1 * len(datas):
        return {**tot, "fonte": None}
    return {**tot, "fonte": "mensal"}


def calcular_periodo(f: dict, energia: dict, venda: Contrato | None, imp: Impostos) -> dict:
    """Refaz a fatura como se toda a energia da casa viesse da rede, com os preços da própria fatura."""
    casa = energia["consumo_kwh"]
    lim = imp.limite_kwh_30dias * f["dias"] / 30
    preco = f["preco_kwh"] or 0
    energia_sem = preco * (min(casa, lim) * (1 + imp.iva_reduzido) + max(casa - lim, 0) * (1 + imp.iva_normal))
    iec_sem = (f.get("iec_kwh") or 0.001) * casa * (1 + imp.iva_normal)
    fixo = (f["potencia_eur"] + f["dgeg_eur"] + f.get("outros_eur", 0)) * (1 + imp.iva_normal) + f["cav_eur"] * (1 + imp.iva_reduzido)
    sem = energia_sem + iec_sem + fixo
    venda_eur = energia["injetado_kwh"] * (venda.preco_kwh if venda else 0)
    return {
        **f,
        "casa_kwh": round(casa, 1),
        "solar_kwh": round(max(casa - f["kwh"], 0), 1),
        "deye_rede_kwh": round(energia["rede_kwh"], 1),
        "injetado_kwh": round(energia["injetado_kwh"], 1),
        "fonte": energia["fonte"],
        "fatura_sem": round(sem, 2),
        "evitado_eur": round(sem - f["total_eur"], 2),
        "venda_eur": round(venda_eur, 2),
        "poupanca_eur": round(sem - f["total_eur"] + venda_eur, 2),
        # a fatura e a Deye medem a rede em sítios diferentes; mais de 15% de diferença merece atenção
        "diferenca_rede": (abs(energia["rede_kwh"] - f["kwh"]) / f["kwh"]) if f["kwh"] else None,
    }
