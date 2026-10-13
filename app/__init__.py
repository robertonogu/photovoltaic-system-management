"""Poupança Solar — aplicação web (Flask)."""
from __future__ import annotations

import hmac
from urllib.parse import urlparse
import logging
import os
import re
import secrets
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, sync
from . import faturas_meo
from .calc import (Contrato, Impostos, calcular, calcular_periodo, em_vigor, energia_no_periodo, faturas_por_mes,
                   resumo)

MESES_LONGOS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
                "outubro", "novembro", "dezembro"]
MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
RE_MES = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
RE_DATA = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def create_app() -> Flask:
    """Cria a app. Se faltar configuração na Vercel, mostra uma página a dizer o quê, em vez de um erro 500."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    faltas = []
    if os.environ.get("VERCEL"):
        if not db.usa_postgres():
            faltas.append(("DATABASE_URL", "A base de dados: em Storage, cria uma Neon (Postgres) e liga-a a este projeto."))
    if faltas:
        return _app_por_configurar(faltas, None)
    try:
        return _criar_app()
    except Exception as e:  # noqa: BLE001 — mostrar a causa em vez de um 500 sem explicação
        logging.getLogger("app").exception("Falha ao arrancar")
        return _app_por_configurar([], f"{type(e).__name__}: {e}")


def _app_por_configurar(faltas, erro):
    app = Flask(__name__)
    texto = re.sub(r"postgres(ql)?://\S+", "postgres://…", erro or "")  # nunca mostrar a ligação com a senha

    @app.route("/", defaults={"caminho": ""})
    @app.route("/<path:caminho>")
    def configurar(caminho):
        from markupsafe import escape
        itens = "".join(f"<li><code>{escape(n)}</code> — {escape(d)}</li>" for n, d in faltas)
        corpo = (f"<p>Falta ligar a base de dados ao projeto na Vercel:</p><ul>{itens}</ul>"
                 "<p>Depois, faz <b>Deployments → ⋯ → Redeploy</b>.</p>") if faltas else (
                 f"<p>A app não conseguiu arrancar:</p><pre>{escape(texto)}</pre>")
        html = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Poupança solar — configuração</title>"
                "<body style='font:16px/1.5 system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#14233a'>"
                f"<h1>☀️ Quase pronto</h1>{corpo}</body>")
        return html, 503

    return app


def _criar_app() -> Flask:
    na_vercel = bool(os.environ.get("VERCEL"))
    # os estáticos estão em public/static: a Vercel serve-os pela CDN, e o Flask serve-os no Docker
    publico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public", "static")
    app = Flask(__name__, static_folder=publico, static_url_path="/static")
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=na_vercel or os.environ.get("COOKIE_SEGURO", "0") == "1")
    db.iniciar()
    # chave das sessões: da variável SECRET_KEY, ou gerada uma vez e guardada na base de dados
    chave = os.environ.get("SECRET_KEY") or db.segredo("secret_key")
    if not chave:
        chave = secrets.token_urlsafe(48)
        db.guardar_segredo("secret_key", chave)
    app.secret_key = chave
    sync.iniciar_agendador()

    def senha_env() -> str:
        return os.environ.get("APP_PASSWORD", "")

    def senha_definida() -> bool:
        return bool(senha_env() or db.segredo("senha_hash"))

    def senha_certa(tentativa: str) -> bool:
        if senha_env():
            return hmac.compare_digest(tentativa, senha_env())
        h = db.segredo("senha_hash")
        return bool(h) and check_password_hash(h, tentativa)

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
        if request.endpoint in ("login", "static", "saude", "cron_sincronizar", "criar_senha", "email_faturas"):
            return
        if not senha_definida():
            return redirect(url_for("criar_senha"))
        if not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        if request.method == "POST":
            token = request.form.get("csrf") or request.headers.get("X-CSRF")
            if not token or not hmac.compare_digest(token, session.get("csrf", "")):
                abort(400, "Pedido inválido (CSRF). Recarrega a página e tenta de novo.")

    @app.context_processor
    def contexto():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return {"csrf": session["csrf"], "com_senha": True, "deye_ok": sync.configurado()}

    @app.route("/criar-senha", methods=["GET", "POST"])
    def criar_senha():
        """Primeira visita: escolher a palavra-passe da app (fica guardada só como hash)."""
        if senha_definida():
            return redirect(url_for("login"))
        erro = None
        if request.method == "POST":
            s1, s2 = request.form.get("senha", ""), request.form.get("senha2", "")
            if len(s1) < 8:
                erro = "Usa pelo menos 8 caracteres."
            elif s1 != s2:
                erro = "As duas palavras-passe não são iguais."
            else:
                db.guardar_segredo("senha_hash", generate_password_hash(s1))
                session.clear()
                session["ok"] = True
                session.permanent = True
                return redirect(url_for("definicoes"))
        return render_template("login.html", erro=erro, criar=True)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not senha_definida():
            return redirect(url_for("criar_senha"))
        erro = None
        if request.method == "POST":
            if senha_certa(request.form.get("senha", "")):
                session.clear()
                session["ok"] = True
                session.permanent = True
                destino = request.args.get("next", "/")
                return redirect(destino if destino.startswith("/") and not destino.startswith("//") else "/")
            erro = "Palavra-passe errada."
        return render_template("login.html", erro=erro, criar=False)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.errorhandler(500)
    def erro_interno(e):
        orig = getattr(e, "original_exception", None) or e
        logging.getLogger("app").error("Erro em %s: %r", request.path, orig)
        texto = re.sub(r"postgres(ql)?://\S+", "postgres://…", f"{type(orig).__name__}: {orig}")[:400]
        from markupsafe import escape
        return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<body style='font:16px/1.5 system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#14233a'>"
                f"<h1>Algo correu mal</h1><p>A página <code>{escape(request.path)}</code> deu um erro:</p>"
                f"<pre style='white-space:pre-wrap;background:#eef3f8;padding:1rem;border-radius:8px'>{escape(texto)}</pre>"
                f"<p>Copia esta mensagem e envia-a. <a href='/'>Voltar ao painel</a></p></body>"), 500

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
            periodos = con.execute("SELECT * FROM faturas_periodo ORDER BY inicio")
        faturas.update(faturas_por_mes(periodos))
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
        """Chamado pelo cron job da Vercel uma vez por dia.

        Com CRON_SECRET definido, exige Authorization: Bearer CRON_SECRET. Sem ele, aceita o cron da Vercel
        mas no máximo uma vez a cada 6 horas (o pior que alguém de fora consegue é uma atualização a mais).
        """
        segredo = os.environ.get("CRON_SECRET", "")
        if segredo:
            if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {segredo}"):
                return {"ok": False, "erro": "não autorizado"}, 401
        else:
            if "vercel-cron" not in request.headers.get("User-Agent", ""):
                return {"ok": False, "erro": "não autorizado"}, 401
            u = sync.ultima()
            if u and u["ok"] and u["quando"] > (datetime.utcnow() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"):
                return {"ok": True, "mensagem": "Já atualizado há pouco."}, 200
        ok, msg = sync.sincronizar()
        return {"ok": ok, "mensagem": msg}, 200 if ok else 500

    @app.post("/sincronizar")
    def sincronizar():
        ok, msg = sync.sincronizar(completo=request.form.get("completo") == "1")
        flash(msg, "ok" if ok else "erro")
        # volta à página de onde veio (só páginas que se podem abrir; nunca o endereço de um formulário)
        de_onde = urlparse(request.referrer or "").path
        destino = next((url_for(e) for e, p in (("definicoes", "/definicoes"), ("meses", "/meses"), ("faturas", "/faturas"))
                        if de_onde.startswith(p)), url_for("painel"))
        return redirect(destino)

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

    # ---------- faturas (por período, como vêm do comercializador) ----------
    def faturas_calculadas():
        d, meses, contratos, _, _, _ = carregar()
        imp = Impostos(float(d["iva_reduzido"]), float(d["iva_normal"]), float(d["limite_iva_kwh_30dias"]))
        objs = [Contrato(c["tipo"], c["empresa"], c["desde"], c["preco_kwh"], c["potencia_eur_dia"] or 0,
                         c["iva_potencia"]) for c in contratos]
        with db.ligar() as con:
            lista = con.execute("SELECT * FROM faturas_periodo ORDER BY inicio DESC")
            dias = {r["data"]: r for r in con.execute("SELECT * FROM dias")} if lista else {}
        out = []
        for f in lista:
            mes_ini = f["inicio"][:7]
            venda = em_vigor(objs, "venda", mes_ini)
            compra = em_vigor(objs, "compra", mes_ini)
            e = energia_no_periodo(f["inicio"], f["fim"], dias, meses)
            r = calcular_periodo(f, e, venda, imp) if e["fonte"] else {**f, "fonte": None}
            # o preço da fatura bate com o contrato em vigor?
            r["contrato"] = compra.empresa if compra else None
            r["preco_diferente"] = bool(compra) and (
                abs((f["preco_kwh"] or 0) - compra.preco_kwh) > 0.00005
                or abs((f["potencia_eur_dia"] or 0) - compra.potencia_eur_dia) > 0.00005)
            out.append(r)
        return out

    def guardar_fatura_pdf(dados: bytes, origem: str) -> tuple[bool, str]:
        try:
            f = faturas_meo.ler_pdf(dados, db.segredo("pdf_senha"))
        except faturas_meo.FaturaIlegivel as e:
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("faturas").exception("Falha a ler PDF")
            return False, f"Não consegui ler o PDF ({type(e).__name__}: {e})."
        f["origem"] = origem
        nova = db.guardar_fatura(f)
        aviso = "" if f.get("confere", True) else " Atenção: o IVA lido não bate certo; confirma os valores."
        return True, (f"Fatura {f['numero']} ({f['inicio'][8:]}/{f['inicio'][5:7]} a {f['fim'][8:]}/{f['fim'][5:7]}) "
                      + ("lida" if nova else "atualizada") + f": {f['kwh']:.0f} kWh, total {f['total_eur']:.2f} €.".replace(".", ",", 1) + aviso)

    @app.route("/faturas", methods=["GET", "POST"])
    def faturas():
        if request.method == "POST":
            ficheiros = [x for x in request.files.getlist("pdf") if x and x.filename]
            if ficheiros:
                for x in ficheiros:
                    ok, msg = guardar_fatura_pdf(x.read(), "pdf")
                    flash(msg if ok else f"{x.filename}: {msg}", "ok" if ok else "erro")
                return redirect(url_for("faturas"))
            # fatura escrita à mão: só período, kWh e total; o resto vem do contrato
            fm = request.form
            inicio, fim = fm.get("inicio", ""), fm.get("fim", "")
            total, kwh = _num(fm.get("total_eur")), _num(fm.get("kwh"))
            if not (RE_DATA.match(inicio) and RE_DATA.match(fim)) or fim < inicio or total is None or kwh is None:
                flash("Indica o início, o fim, os kWh e o total da fatura.", "erro")
                return redirect(url_for("faturas"))
            _, _, contratos, _, _, _ = carregar()
            objs = [Contrato(c["tipo"], c["empresa"], c["desde"], c["preco_kwh"], c["potencia_eur_dia"] or 0,
                             c["iva_potencia"]) for c in contratos]
            compra = em_vigor(objs, "compra", inicio[:7])
            n = (date.fromisoformat(fim) - date.fromisoformat(inicio)).days + 1
            pot = compra.potencia_eur_dia if compra else 0
            preco = compra.preco_kwh if compra else 0
            db.guardar_fatura({
                "numero": f"manual-{inicio}", "comercializador": compra.empresa if compra else None, "emitida": None,
                "inicio": inicio, "fim": fim, "dias": n, "kwh": kwh, "preco_kwh": preco, "potencia_eur_dia": pot,
                "energia_eur": round(kwh * preco, 2), "potencia_eur": round(pot * n, 2),
                "cav_eur": round(2.85 * 12 / 365.25 * n, 2), "iec_eur": round(0.001 * kwh, 2), "iec_kwh": 0.001,
                "dgeg_eur": 0.07, "outros_eur": 0, "iva_eur": None, "total_eur": total, "origem": "manual",
            })
            flash("Fatura guardada.", "ok")
            return redirect(url_for("faturas"))
        token = db.segredo("email_token")
        with db.ligar() as con:
            antigas = con.execute("SELECT * FROM faturas ORDER BY mes DESC")
        return render_template("faturas.html", faturas=faturas_calculadas(), antigas=antigas,
                               hoje=date.today().isoformat(), email_ativo=bool(token))

    @app.post("/faturas/<path:numero>/apagar")
    def apagar_fatura(numero):
        with db.ligar() as con:
            con.execute("DELETE FROM faturas_periodo WHERE numero = ?", (numero,))
            con.execute("DELETE FROM faturas WHERE mes = ?", (numero,))  # faturas mensais antigas
        flash("Fatura apagada.", "ok")
        return redirect(url_for("faturas"))

    @app.post("/faturas/<path:numero>/criar-contrato")
    def contrato_da_fatura(numero):
        with db.ligar() as con:
            f = con.um("SELECT * FROM faturas_periodo WHERE numero = ?", (numero,))
            if f:
                con.execute("INSERT INTO contratos (tipo, empresa, desde, preco_kwh, potencia_eur_dia, iva_potencia, notas) "
                            "VALUES ('compra', ?, ?, ?, ?, ?, ?)",
                            (f["comercializador"] or "MEO Energia", f["inicio"], f["preco_kwh"], f["potencia_eur_dia"] or 0,
                             float(db.definicoes()["iva_normal"]), f"Preços da fatura {numero}"))
        flash("Contrato criado com os preços da fatura." if f else "Fatura não encontrada.", "ok" if f else "erro")
        return redirect(url_for("contratos"))

    # ---------- faturas por email ----------
    @app.post("/api/email/faturas/<token>")
    def email_faturas(token):
        """Recebe emails reencaminhados (CloudMailin, Mailgun, Postmark…) e lê os PDF em anexo."""
        certo = db.segredo("email_token")
        if not certo or not hmac.compare_digest(token, certo):
            return {"ok": False}, 404
        import base64
        anexos, assunto, remetente, texto = [], "", "", ""
        j = request.get_json(silent=True)
        if j:  # formato JSON (CloudMailin "JSON normalized", Postmark)
            h = j.get("headers") or {}
            assunto = h.get("subject") or h.get("Subject") or j.get("Subject") or ""
            remetente = h.get("from") or h.get("From") or j.get("From") or (j.get("envelope") or {}).get("from") or ""
            texto = j.get("plain") or j.get("TextBody") or ""
            for a in (j.get("attachments") or j.get("Attachments") or []):
                nome = a.get("file_name") or a.get("Name") or ""
                conteudo = a.get("content") or a.get("Content")
                if conteudo and (nome.lower().endswith(".pdf") or "pdf" in (a.get("content_type") or a.get("ContentType") or "")):
                    anexos.append((nome, base64.b64decode(conteudo)))
        else:  # formato multipart (CloudMailin multipart, Mailgun)
            fm = request.form
            assunto = fm.get("headers[subject]") or fm.get("subject") or fm.get("Subject") or ""
            remetente = fm.get("headers[from]") or fm.get("from") or fm.get("sender") or ""
            texto = fm.get("plain") or fm.get("body-plain") or ""
            for _, x in request.files.items(multi=True):
                if x.filename.lower().endswith(".pdf") or x.mimetype == "application/pdf":
                    anexos.append((x.filename, x.read()))
        resultados = [guardar_fatura_pdf(dados, "email") for _, dados in anexos]
        resumo_txt = "; ".join(m for _, m in resultados) or "Sem PDF em anexo."
        with db.ligar() as con:
            con.execute("INSERT INTO emails_recebidos (quando, remetente, assunto, resumo, resultado) VALUES (?,?,?,?,?)",
                        (db.agora(), str(remetente)[:200], str(assunto)[:200], texto[:800], resumo_txt[:500]))
            con.execute("DELETE FROM emails_recebidos WHERE id NOT IN (SELECT id FROM emails_recebidos ORDER BY id DESC LIMIT 20)")
        return {"ok": True, "faturas": [m for ok, m in resultados if ok]}, 200

    @app.post("/definicoes/email")
    def ativar_email():
        if request.form.get("acao") == "desligar":
            db.guardar_segredo("email_token", None)
            flash("Receção de faturas por email desligada.", "ok")
        else:
            db.guardar_segredo("email_token", secrets.token_urlsafe(24))
            flash("Endereço de receção criado. Copia-o para o CloudMailin.", "ok")
        return redirect(url_for("definicoes") + "#email")

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
        return pagina_definicoes(None)

    def pagina_definicoes(teste):
        cred = sync.credenciais()
        return render_template("definicoes.html", defs=db.definicoes(), ultima=sync.ultima(), teste=teste,
                               deye_origem=cred["origem"] if cred else None,
                               deye_app_id=db.segredo("deye_app_id") or "", deye_email=db.segredo("deye_email") or "",
                               senha_por_variavel=bool(senha_env()),
                               email_url=((request.url_root.rstrip("/").replace("http://", "https://", 1)
                                           if os.environ.get("VERCEL") else request.url_root.rstrip("/"))
                                          + url_for("email_faturas", token=db.segredo("email_token")))
                               if db.segredo("email_token") else None,
                               emails=emails_recentes())

    def emails_recentes():
        with db.ligar() as con:
            return con.execute("SELECT * FROM emails_recebidos ORDER BY id DESC LIMIT 5")

    @app.get("/definicoes/testar-deye")
    @app.get("/definicoes/deye")
    @app.get("/definicoes/senha")
    @app.get("/definicoes/email")
    @app.get("/sincronizar")
    def voltar_definicoes():
        return redirect(url_for("definicoes"))

    @app.post("/definicoes/testar-deye")
    def testar_deye():
        return pagina_definicoes(sync.testar_ligacao())

    @app.post("/definicoes/deye")
    def guardar_deye():
        f = request.form
        if f.get("acao") == "apagar":
            for k in ("app_id", "app_secret", "email", "password_sha256"):
                db.guardar_segredo("deye_" + k, None)
            flash("Dados da Deye apagados da app.", "ok")
            return redirect(url_for("definicoes"))
        app_id, email = f.get("app_id", "").strip(), f.get("email", "").strip()
        app_secret, password = f.get("app_secret", "").strip(), f.get("password", "")
        if not app_id or not email:
            flash("Preenche o App ID e o email da Deye.", "erro")
            return redirect(url_for("definicoes"))
        if not app_secret and not db.segredo("deye_app_secret"):
            flash("Falta o App Secret.", "erro")
            return redirect(url_for("definicoes"))
        if not password and not db.segredo("deye_password_sha256"):
            flash("Falta a palavra-passe da Deye.", "erro")
            return redirect(url_for("definicoes"))
        db.guardar_segredo("deye_app_id", app_id)
        db.guardar_segredo("deye_email", email)
        if app_secret:
            db.guardar_segredo("deye_app_secret", app_secret)
        if password:  # só guardamos o SHA-256, que é o que a Deye pede
            import hashlib
            db.guardar_segredo("deye_password_sha256", hashlib.sha256(password.encode("utf-8")).hexdigest())
        return pagina_definicoes(sync.testar_ligacao())

    @app.post("/definicoes/senha")
    def mudar_senha():
        if senha_env():
            flash("A palavra-passe está definida na variável APP_PASSWORD do servidor; muda-a lá.", "erro")
        elif not senha_certa(request.form.get("atual", "")):
            flash("A palavra-passe atual não está certa.", "erro")
        elif len(request.form.get("nova", "")) < 8:
            flash("A nova palavra-passe precisa de pelo menos 8 caracteres.", "erro")
        else:
            db.guardar_segredo("senha_hash", generate_password_hash(request.form["nova"]))
            flash("Palavra-passe mudada.", "ok")
        return redirect(url_for("definicoes"))

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
