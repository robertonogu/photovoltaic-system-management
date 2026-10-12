"""Poupança Solar — aplicação web (Flask)."""
from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from datetime import date

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for)

from . import db, sync
from .calc import Contrato, Impostos, calcular, resumo

MESES_LONGOS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
                "outubro", "novembro", "dezembro"]
MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
RE_MES = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
RE_DATA = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def create_app() -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    na_vercel = bool(os.environ.get("VERCEL"))
    # os estáticos estão em public/static: a Vercel serve-os pela CDN, e o Flask serve-os no Docker
    publico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public", "static")
    app = Flask(__name__, static_folder=publico, static_url_path="/static")
    if na_vercel and not os.environ.get("SECRET_KEY"):
        raise RuntimeError("Falta a variável SECRET_KEY nas definições do projeto na Vercel.")
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=na_vercel or os.environ.get("COOKIE_SEGURO", "0") == "1")
    db.iniciar()
    sync.iniciar_agendador()
    senha = os.environ.get("APP_PASSWORD", "")

    # ---------- formatação ----------
    @app.template_filter("eur")
    def f_eur(v):
        if v is None:
            return "—"
        s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
        return f"{s} €"

    @app.template_filter("kwh")
    def f_kwh(v):
        return "—" if v is None else f"{v:,.0f}".replace(",", " ")

    @app.template_filter("pct")
    def f_pct(v):
        return "—" if v is None else f"{v * 100:.0f} %".replace(".", ",")

    @app.template_filter("mes")
    def f_mes(v):
        if not v or not RE_MES.match(str(v)):
            return v or "—"
        a, m = v.split("-")
        return f"{MESES_PT[int(m) - 1]} {a}"

    @app.template_filter("mes_longo")
    def f_mes_longo(v):
        if not v or not RE_MES.match(str(v)):
            return v or "—"
        a, m = v.split("-")
        return f"{MESES_LONGOS[int(m) - 1]} de {a}"

    @app.template_filter("so_mes")
    def f_so_mes(v):
        return MESES_LONGOS[int(v.split("-")[1]) - 1] if v and RE_MES.match(str(v)) else (v or "—")

    @app.template_filter("duracao")
    def f_duracao(meses):
        if meses is None:
            return "—"
        anos, m = divmod(int(meses), 12)
        partes = []
        if anos:
            partes.append(f"{anos} ano" + ("s" if anos > 1 else ""))
        if m:
            partes.append(f"{m} " + ("mês" if m == 1 else "meses"))
        return " e ".join(partes) or "menos de um mês"

    @app.template_filter("eur0")
    def f_eur0(v):
        return "—" if v is None else f"{v:,.0f}".replace(",", " ") + " €"

    @app.template_global()
    def ponto_arco(p, cx=220, cy=220, r=180):
        """Posição do sol no arco: p=0 nascer (instalação), p=1 pôr do sol (pago)."""
        import math
        p = max(0.0, min(1.0, float(p or 0)))
        t = math.pi * (1 - p)
        return round(cx + r * math.cos(t), 2), round(cy - r * math.sin(t), 2)

    @app.template_filter("simples")
    def f_simples(v):
        return f"{round(float(v), 4):g}".replace(".", ",")

    @app.template_filter("num")
    def f_num(v, casas=4):
        return "—" if v is None else f"{v:.{casas}f}".replace(".", ",")

    # ---------- segurança ----------
    @app.before_request
    def proteger():
        if request.endpoint in ("login", "static", "saude", "cron_sincronizar"):
            return
        if senha and not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        if request.method == "POST":
            token = request.form.get("csrf") or request.headers.get("X-CSRF")
            if not token or not hmac.compare_digest(token, session.get("csrf", "")):
                abort(400, "Pedido inválido (CSRF). Recarrega a página e tenta de novo.")

    @app.context_processor
    def contexto():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return {"csrf": session["csrf"], "com_senha": bool(senha), "deye_ok": sync.configurado()}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not senha:
            return redirect(url_for("painel"))
        erro = None
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("senha", ""), senha):
                session.clear()
                session["ok"] = True
                session.permanent = True
                destino = request.args.get("next", "/")
                return redirect(destino if destino.startswith("/") and not destino.startswith("//") else "/")
            erro = "Palavra-passe errada."
        return render_template("login.html", erro=erro)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/saude")
    def saude():
        return {"ok": True}

    # ---------- dados ----------
    def carregar():
        d = db.definicoes()
        with db.ligar() as con:
            meses = con.execute("SELECT * FROM meses ORDER BY mes")
            contratos = con.execute("SELECT * FROM contratos ORDER BY desde DESC, id DESC")
            faturas = {r["mes"]: r["total_eur"] for r in con.execute("SELECT mes, total_eur FROM faturas")}
        imp = Impostos(float(d["iva_reduzido"]), float(d["iva_normal"]), float(d["limite_iva_kwh_30dias"]))
        objs = [Contrato(c["tipo"], c["empresa"], c["desde"], c["preco_kwh"], c["potencia_eur_dia"] or 0,
                         c["iva_potencia"]) for c in contratos]
        res = calcular(meses, objs, faturas, imp, date.today())
        inv = float(d["investimento_eur"])
        return d, meses, contratos, faturas, res, resumo(res, inv)

    @app.route("/")
    def painel():
        d, meses, contratos, faturas, res, rsm = carregar()
        return render_template("painel.html", res=res, rsm=rsm, defs=d, ultima=sync.ultima())

    @app.route("/api/dados")
    def api_dados():
        d, meses, contratos, faturas, res, rsm = carregar()
        return jsonify({"resumo": rsm, "meses": res, "contratos": contratos, "definicoes": d})

    @app.get("/api/cron/sincronizar")
    def cron_sincronizar():
        """Chamado pelo cron job da Vercel uma vez por dia, com Authorization: Bearer CRON_SECRET."""
        segredo = os.environ.get("CRON_SECRET", "")
        if not segredo or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {segredo}"):
            return {"ok": False, "erro": "não autorizado"}, 401
        ok, msg = sync.sincronizar()
        return {"ok": ok, "mensagem": msg}, 200 if ok else 500

    @app.post("/sincronizar")
    def sincronizar():
        ok, msg = sync.sincronizar(completo=request.form.get("completo") == "1")
        flash(msg, "ok" if ok else "erro")
        return redirect(request.referrer or url_for("painel"))

    # ---------- contratos ----------
    @app.route("/contratos", methods=["GET", "POST"])
    def contratos():
        if request.method == "POST":
            f = request.form
            try:
                tipo = f["tipo"]
                assert tipo in ("compra", "venda"), "Tipo tem de ser compra ou venda."
                empresa = f["empresa"].strip()
                assert empresa, "Falta o nome da empresa."
                desde = f["desde"]
                assert RE_DATA.match(desde), "Data de início inválida."
                preco = _num(f["preco_kwh"])
                assert preco is not None and preco >= 0, "Preço do kWh inválido."
                pot = _num(f.get("potencia_eur_dia")) or 0
                iva_pot = _num(f.get("iva_potencia"))
                iva_pot = iva_pot / 100 if iva_pot is not None and iva_pot > 1 else iva_pot
            except (KeyError, AssertionError) as e:
                flash(str(e) or "Dados em falta.", "erro")
                return redirect(url_for("contratos"))
            with db.ligar() as con:
                con.execute("INSERT INTO contratos (tipo, empresa, desde, preco_kwh, potencia_eur_dia, iva_potencia, notas) "
                            "VALUES (?,?,?,?,?,?,?)", (tipo, empresa, desde, preco, pot, iva_pot, f.get("notas", "").strip()))
            flash(f"Contrato de {tipo} com {empresa} adicionado.", "ok")
            return redirect(url_for("contratos"))
        _, _, lista, _, res, _ = carregar()
        hoje = date.today().strftime("%Y-%m")
        ativos = {}
        for c in sorted(lista, key=lambda c: c["desde"]):
            if c["desde"][:7] <= hoje:
                ativos[c["tipo"]] = c["id"]
        return render_template("contratos.html", contratos=lista, ativos=set(ativos.values()), hoje=date.today().isoformat())

    @app.post("/contratos/<int:cid>/apagar")
    def apagar_contrato(cid):
        with db.ligar() as con:
            con.execute("DELETE FROM contratos WHERE id = ?", (cid,))
        flash("Contrato apagado.", "ok")
        return redirect(url_for("contratos"))

    # ---------- faturas ----------
    @app.route("/faturas", methods=["GET", "POST"])
    def faturas():
        if request.method == "POST":
            mes, total = request.form.get("mes", ""), _num(request.form.get("total_eur"))
            if not RE_MES.match(mes) or total is None:
                flash("Indica o mês e o total da fatura.", "erro")
            else:
                with db.ligar() as con:
                    con.execute("INSERT INTO faturas (mes, total_eur, notas) VALUES (?,?,?) ON CONFLICT (mes) DO UPDATE "
                                "SET total_eur = excluded.total_eur, notas = excluded.notas",
                                (mes, total, request.form.get("notas", "").strip()))
                flash("Fatura guardada.", "ok")
            return redirect(url_for("faturas"))
        with db.ligar() as con:
            lista = con.execute("SELECT * FROM faturas ORDER BY mes DESC")
        return render_template("faturas.html", faturas=lista, mes_passado=_mes_passado())

    @app.post("/faturas/<mes>/apagar")
    def apagar_fatura(mes):
        with db.ligar() as con:
            con.execute("DELETE FROM faturas WHERE mes = ?", (mes,))
        flash("Fatura apagada.", "ok")
        return redirect(url_for("faturas"))

    # ---------- meses (dados de energia) ----------
    @app.route("/meses", methods=["GET", "POST"])
    def meses():
        if request.method == "POST":
            f = request.form
            mes = f.get("mes", "")
            rede, direto, bat, inj = (_num(f.get(k)) for k in ("rede_kwh", "direto_kwh", "bateria_kwh", "injetado_kwh"))
            if not RE_MES.match(mes) or None in (rede, direto, bat, inj):
                flash("Preenche o mês e os quatro valores em kWh.", "erro")
            else:
                with db.ligar() as con:
                    con.execute("""INSERT INTO meses (mes, consumo_kwh, rede_kwh, injetado_kwh, bateria_kwh, origem, atualizado)
                                   VALUES (?,?,?,?,?,'manual',?)
                                   ON CONFLICT (mes) DO UPDATE SET consumo_kwh=excluded.consumo_kwh, rede_kwh=excluded.rede_kwh,
                                     injetado_kwh=excluded.injetado_kwh, bateria_kwh=excluded.bateria_kwh,
                                     producao_kwh=NULL, carga_bateria_kwh=NULL, origem='manual', atualizado=excluded.atualizado""",
                                (mes, round(rede + direto + bat, 2), rede, inj, bat, db.agora()))
                flash("Mês guardado. Fica com os teus valores; a Deye não o altera.", "ok")
            return redirect(url_for("meses"))
        with db.ligar() as con:
            lista = con.execute("SELECT * FROM meses ORDER BY mes DESC")
        for m in lista:
            m["direto_kwh"] = max((m["consumo_kwh"] or 0) - (m["rede_kwh"] or 0) - (m["bateria_kwh"] or 0), 0)
        return render_template("meses.html", meses=lista, mes_passado=_mes_passado(), ultima=sync.ultima())

    @app.post("/meses/<mes>/apagar")
    def apagar_mes(mes):
        with db.ligar() as con:
            con.execute("DELETE FROM meses WHERE mes = ?", (mes,))
        flash("Mês apagado. Na próxima sincronização volta a vir da Deye.", "ok")
        return redirect(url_for("meses"))

    # ---------- definições ----------
    @app.route("/definicoes", methods=["GET", "POST"])
    def definicoes():
        if request.method == "POST":
            f = request.form
            erros = []
            for chave in ("investimento_eur", "iva_reduzido", "iva_normal", "limite_iva_kwh_30dias"):
                v = _num(f.get(chave))
                if chave.startswith("iva") and v is not None and v > 1:
                    v = v / 100  # aceitar 23 ou 0,23
                if v is None or v < 0:
                    erros.append(chave)
                else:
                    db.guardar_definicao(chave, str(v))
            if RE_MES.match(f.get("primeiro_mes", "")):
                db.guardar_definicao("primeiro_mes", f["primeiro_mes"])
            else:
                erros.append("primeiro_mes")
            flash("Definições guardadas." if not erros else "Alguns valores não eram válidos e não foram guardados.",
                  "ok" if not erros else "erro")
            return redirect(url_for("definicoes"))
        return render_template("definicoes.html", defs=db.definicoes(), ultima=sync.ultima(), teste=None)

    @app.post("/definicoes/testar-deye")
    def testar_deye():
        return render_template("definicoes.html", defs=db.definicoes(), ultima=sync.ultima(), teste=sync.testar_ligacao())

    return app


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace("€", "").replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _mes_passado() -> str:
    h = date.today().replace(day=1)
    a, m = (h.year, h.month - 1) if h.month > 1 else (h.year - 1, 12)
    return f"{a:04d}-{m:02d}"
