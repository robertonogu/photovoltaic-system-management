"""Sincronização com a Deye Cloud: manual (botão) e automática (uma vez por dia)."""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta

from . import db
from .calc import somar_meses
from .deye import DeyeCloud, DeyeErro

log = logging.getLogger("sync")
_lock = threading.Lock()


def credenciais() -> dict | None:
    """Credenciais da Deye: primeiro das variáveis de ambiente, senão as guardadas em Definições."""
    e = os.environ
    if all(e.get(k) for k in ("DEYE_APP_ID", "DEYE_APP_SECRET", "DEYE_EMAIL", "DEYE_PASSWORD")):
        return {"app_id": e["DEYE_APP_ID"], "app_secret": e["DEYE_APP_SECRET"], "login": e["DEYE_EMAIL"],
                "password": e["DEYE_PASSWORD"], "origem": "variaveis"}
    try:
        g = {k: db.segredo("deye_" + k) for k in ("app_id", "app_secret", "email", "password_sha256")}
    except Exception:  # noqa: BLE001 — BD ainda não pronta
        return None
    if all(g.values()):
        return {"app_id": g["app_id"], "app_secret": g["app_secret"], "login": g["email"],
                "password_sha256": g["password_sha256"], "origem": "app"}
    return None


def configurado() -> bool:
    return credenciais() is not None


def cliente() -> DeyeCloud:
    c = credenciais()
    if not c:
        raise DeyeErro("A ligação à Deye não está configurada.")
    return DeyeCloud(
        base_url=os.environ.get("DEYE_BASE_URL", "https://eu1-developer.deyecloud.com/v1.0"),
        app_id=c["app_id"], app_secret=c["app_secret"], login=c["login"],
        password=c.get("password"), password_sha256=c.get("password_sha256"),
        station_id=os.environ.get("DEYE_STATION_ID") or None,
    )


def _mes_atual() -> str:
    return datetime.now().strftime("%Y-%m")


def sincronizar(completo: bool = False) -> tuple[bool, str]:
    """Vai buscar os totais mensais à Deye e grava-os. Meses escritos à mão não são alterados.

    Por defeito (todos os dias) só lê o mês atual e o anterior: o anterior pode ainda receber
    dados atrasados do logger nos primeiros dias do mês. Com completo=True relê o histórico todo
    (também acontece sozinho na primeira vez).
    """
    if not configurado():
        return False, "A ligação à Deye não está configurada. Preenche os dados em Definições."
    if not _lock.acquire(blocking=False):
        return False, "Já está a decorrer uma atualização."
    try:
        primeiro = db.definicoes().get("primeiro_mes", "2026-07")
        with db.ligar() as con:
            ja_tem_deye = con.um("SELECT COUNT(*) AS n FROM meses WHERE origem = 'deye'")["n"] > 0
        inicio = primeiro if (completo or not ja_tem_deye) else max(primeiro, somar_meses(_mes_atual(), -1))
        meses = cliente().historico_mensal(inicio)
        atualizados, mantidos = [], []
        with db.ligar() as con:
            manuais = {r["mes"] for r in con.execute("SELECT mes FROM meses WHERE origem = 'manual'")}
            for m in meses:
                if m["mes"] < primeiro:
                    continue
                if m["mes"] in manuais:
                    mantidos.append(m["mes"])
                    continue
                con.execute(
                    """INSERT INTO meses (mes, consumo_kwh, rede_kwh, injetado_kwh, producao_kwh, bateria_kwh,
                                          carga_bateria_kwh, origem, atualizado)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'deye', ?)
                       ON CONFLICT (mes) DO UPDATE SET consumo_kwh = excluded.consumo_kwh,
                         rede_kwh = excluded.rede_kwh, injetado_kwh = excluded.injetado_kwh,
                         producao_kwh = excluded.producao_kwh, bateria_kwh = excluded.bateria_kwh,
                         carga_bateria_kwh = excluded.carga_bateria_kwh, atualizado = excluded.atualizado""",
                    (m["mes"], m["consumo_kwh"], m["rede_kwh"], m["injetado_kwh"], m["producao_kwh"],
                     m["bateria_kwh"], m["carga_bateria_kwh"], db.agora()))
                atualizados.append(m["mes"])
        if atualizados:
            msg = "Atualizado da Deye: " + ", ".join(_rotulo(x) for x in atualizados) + "."
        else:
            msg = "A Deye não devolveu meses novos."
        if mantidos:
            msg += " Mantidos os teus valores à mão de " + ", ".join(_rotulo(x) for x in mantidos) + "."
        _registar(True, msg)
        return True, msg
    except (DeyeErro, KeyError, ValueError) as e:
        _registar(False, str(e))
        return False, str(e)
    except Exception as e:  # noqa: BLE001 — registar qualquer falha inesperada
        log.exception("Falha na sincronização")
        _registar(False, f"Erro inesperado: {e}")
        return False, f"Erro inesperado: {e}"
    finally:
        _lock.release()


def testar_ligacao() -> dict:
    """Entra na Deye e devolve as instalações e os totais do mês anterior, sem gravar nada."""
    if not configurado():
        return {"ok": False, "erro": "Preenche primeiro os dados da Deye em Definições."}
    try:
        c = cliente()
        c.autenticar()
        estacoes = c.estacoes()
        anterior = somar_meses(_mes_atual(), -1)
        dados = c.historico_mensal(anterior, anterior)
        return {"ok": True, "estacoes": estacoes, "usada": c.estacao(), "mes": anterior,
                "dados": dados[0] if dados else None}
    except DeyeErro as e:
        return {"ok": False, "erro": str(e)}
    except Exception as e:  # noqa: BLE001
        log.exception("Falha ao testar a Deye")
        return {"ok": False, "erro": f"Erro inesperado: {e}"}


_MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def _rotulo(mes: str) -> str:
    a, m = mes.split("-")
    return f"{_MESES[int(m) - 1]} {a}"


def _registar(ok: bool, mensagem: str) -> None:
    with db.ligar() as con:
        con.execute("INSERT INTO sincronizacoes (quando, ok, mensagem) VALUES (?, ?, ?)", (db.agora(), int(ok), mensagem))
        con.execute("DELETE FROM sincronizacoes WHERE id NOT IN (SELECT id FROM sincronizacoes ORDER BY id DESC LIMIT 50)")


def ultima() -> dict | None:
    with db.ligar() as con:
        return con.um("SELECT quando, ok, mensagem FROM sincronizacoes ORDER BY id DESC LIMIT 1")


def iniciar_agendador() -> None:
    """Corre a sincronização uma vez por dia, à hora SYNC_HOUR (por defeito 6h)."""
    # na Vercel não há processos sempre ligados: quem sincroniza é o cron job (rota /api/cron/sincronizar)
    if not configurado() or os.environ.get("SYNC_DESLIGADO") == "1" or os.environ.get("VERCEL"):
        return
    hora = int(os.environ.get("SYNC_HOUR", "6"))

    def ciclo():
        time.sleep(15)
        while True:
            u = ultima()
            agora = datetime.now()
            quando = datetime.fromisoformat(u["quando"]) if u else None  # UTC no SQLite
            espera = timedelta(hours=23) if u and u["ok"] else timedelta(hours=2)  # falhou: tenta de novo mais cedo
            precisa = quando is None or datetime.utcnow() - quando > espera
            if precisa and agora.hour >= hora:
                ok, msg = sincronizar()
                log.info("Sincronização automática: %s", msg)
            time.sleep(600)

    threading.Thread(target=ciclo, daemon=True, name="sync-deye").start()
