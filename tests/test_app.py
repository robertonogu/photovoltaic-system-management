"""Testes: cálculo, páginas e sincronização contra uma Deye Cloud simulada."""
import json
import os
import re
import threading
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.calc import Contrato, Impostos, calcular, resumo, somar_meses

MEO = Contrato("compra", "MEO Energia", "2026-07-01", 0.1499, 0.47, 0.23)
VENDA = Contrato("venda", "X", "2026-07-01", 0.06)
MESES = [  # (mês, rede, painéis direto, bateria, injetado) — os teus valores
    ("2026-07", 15.79, 101.79, 98.79, 40.59),
    ("2026-08", 48.19, 81.70, 105.89, 187.49),
    ("2026-09", 43.80, 110.79, 126.40, 219.89),
]


def linhas():
    return [{"mes": m, "consumo_kwh": r + d + b, "rede_kwh": r, "injetado_kwh": i, "bateria_kwh": b} for m, r, d, b, i in MESES]


def test_calculo_julho_a_mao():
    res = calcular(linhas(), [MEO, VENDA], {}, Impostos(), date(2026, 10, 12))
    jul = res[0]
    consumo = 15.79 + 101.79 + 98.79
    lim = 200 * 31 / 30
    pot = 0.47 * 31 * 1.23
    sem = 0.1499 * (lim * 1.06 + (consumo - lim) * 1.23) + pot
    com = 0.1499 * 15.79 * 1.06 + pot
    assert jul["fatura_sem"] == round(sem, 2) == 52.55
    assert jul["fatura_com"] == round(com, 2) == 20.43
    assert jul["poupanca_eur"] == round(sem - com + 40.59 * 0.06, 2) == 34.55
    assert [r["poupanca_eur"] for r in res] == [34.55, 41.80, 52.95]
    assert res[-1]["acumulado_eur"] == 129.30


def test_resumo_e_break_even():
    res = calcular(linhas(), [MEO, VENDA], {}, Impostos(), date(2026, 10, 12))
    r = resumo(res, 3979.32)
    assert r["total"] == 129.30 and r["meses"] == 3 and r["media"] == 43.10
    assert r["previsao"] == "2034-03"


def test_contrato_novo_so_vale_para_a_frente():
    novo = Contrato("compra", "EDP", "2026-09-01", 0.20, 0.47, 0.23)
    res = calcular(linhas(), [MEO, VENDA, novo], {}, Impostos(), date(2026, 10, 12))
    assert [r["empresa_compra"] for r in res] == ["MEO Energia", "MEO Energia", "EDP"]
    assert res[0]["poupanca_eur"] == 34.55  # julho não muda


def test_fatura_real_substitui_estimativa():
    res = calcular(linhas(), [MEO, VENDA], {"2026-09": 30.0}, Impostos(), date(2026, 10, 12))
    set_ = res[2]
    assert set_["fatura_com"] == 30.0
    assert set_["fatura_sem"] == round(30.0 + 64.05 - 24.30, 2)
    assert set_["poupanca_eur"] == 52.95  # a poupança não depende da fatura real


def test_mes_em_curso_fora_da_media():
    l = linhas() + [{"mes": "2026-10", "consumo_kwh": 100, "rede_kwh": 20, "injetado_kwh": 50, "bateria_kwh": 40}]
    res = calcular(l, [MEO, VENDA], {}, Impostos(), date(2026, 10, 12))
    assert res[-1]["em_curso"] and res[-1]["dias"] == 12
    assert resumo(res, 3979.32)["media"] == 43.10


def test_somar_meses():
    assert somar_meses("2026-12", 1) == "2027-01" and somar_meses("2026-01", 11) == "2026-12"


# ---------- Deye simulada ----------
class FalsaDeye(BaseHTTPRequestHandler):
    pedidos = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FalsaDeye.pedidos.append((self.path, corpo, self.headers.get("Authorization")))
        if self.path.startswith("/v1.0/account/token"):
            ok = corpo.get("email") == "eu@x.pt" and re.fullmatch(r"[0-9a-f]{64}", corpo["password"])
            r = {"success": True, "accessToken": "abc"} if ok else {"success": False, "msg": "auth failed"}
        elif self.path == "/v1.0/station/list":
            r = {"success": True, "stationList": [{"id": 777, "name": "Casa"}], "total": 1}
        elif self.path == "/v1.0/station/history" and corpo["granularity"] == 2:
            from datetime import date as _d, timedelta as _t
            ini, fim = _d.fromisoformat(corpo["startAt"]), _d.fromisoformat(corpo["endAt"])
            itens, d = [], ini
            while d < fim:  # 10 kWh por dia em casa, 2 da rede, 7 injetados
                itens.append({"year": d.year, "month": d.month, "day": d.day, "consumptionValue": 10,
                              "purchaseValue": 2, "gridValue": 7, "generationValue": 15, "dischargeValue": 4})
                d += _t(days=1)
            r = {"success": True, "stationDataItems": itens}
        elif self.path == "/v1.0/station/history":
            assert corpo["granularity"] == 3 and corpo["stationId"] == 777
            r = {"success": True, "stationDataItems": [
                {"year": 2026, "month": 9, "generationValue": 500, "consumptionValue": 300, "gridValue": 210,
                 "purchaseValue": 40, "chargeValue": 130, "dischargeValue": 120},
                {"year": 2026, "month": 10, "generationValue": 150, "consumptionValue": 110, "gridValue": 60,
                 "purchaseValue": 15, "chargeValue": 45, "dischargeValue": 40},
            ]}
        else:
            r = {"success": False, "msg": "?"}
        dados = json.dumps(r).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


PG = os.environ.get("TEST_PG_URL")  # ex.: postgresql://postgres@127.0.0.1:5433/postgres


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG else []))
def cliente(request, tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), FalsaDeye)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for k in ("DATABASE_URL", "POSTGRES_URL", "VERCEL"):
        monkeypatch.delenv(k, raising=False)
    nome_bd = None
    if request.param == "postgres":
        import psycopg
        nome_bd = "t_" + uuid.uuid4().hex[:10]
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"CREATE DATABASE {nome_bd}")
        monkeypatch.setenv("DATABASE_URL", PG.rsplit("/", 1)[0] + "/" + nome_bd)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_PASSWORD", "segredo")
    monkeypatch.setenv("SYNC_DESLIGADO", "1")
    monkeypatch.setenv("CRON_SECRET", "cron-segredo-123456")
    monkeypatch.setenv("DEYE_BASE_URL", f"http://127.0.0.1:{srv.server_port}/v1.0")
    for k, v in {"DEYE_APP_ID": "id", "DEYE_APP_SECRET": "s", "DEYE_EMAIL": "eu@x.pt", "DEYE_PASSWORD": "pw"}.items():
        monkeypatch.setenv(k, v)
    from app import create_app
    app = create_app()
    app.testing = True
    c = app.test_client()
    yield c
    srv.shutdown()
    if nome_bd:
        import psycopg
        with psycopg.connect(PG, autocommit=True) as con:
            con.execute(f"DROP DATABASE {nome_bd} WITH (FORCE)")


def csrf(c, url="/"):
    html = c.get(url).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def entrar(c):
    assert c.get("/").status_code == 302
    assert c.post("/login", data={"senha": "errada"}).status_code == 200
    r = c.post("/login", data={"senha": "segredo"})
    assert r.status_code == 302


def test_paginas_e_formularios(cliente):
    c = cliente
    entrar(c)
    html = c.get("/").get_data(as_text=True)
    assert "129,30 €" in html and "mar 2034" in html
    for url in ("/meses", "/contratos", "/faturas", "/definicoes"):
        assert c.get(url).status_code == 200, url

    t = csrf(c)
    assert c.post("/contratos", data={"tipo": "compra", "empresa": "EDP"}).status_code == 400  # sem CSRF
    c.post("/contratos", data={"csrf": t, "tipo": "compra", "empresa": "EDP", "desde": "2026-09-01",
                               "preco_kwh": "0,20", "potencia_eur_dia": "0,47", "iva_potencia": "23"})
    dados = c.get("/api/dados").get_json()
    assert [m["empresa_compra"] for m in dados["meses"]] == ["MEO Energia", "MEO Energia", "EDP"]

    # faturas e meses já não se escrevem à mão
    assert c.post("/faturas", data={"csrf": t, "inicio": "2026-08-01"}).status_code == 405
    assert c.post("/meses", data={"csrf": t, "mes": "2026-08"}).status_code == 405
    assert 'type="file"' not in c.get("/faturas").get_data(as_text=True)

    assert c.get("/api/dados").get_json()["definicoes"]["data_investimento"] == "2026-07-01"
    c.post("/definicoes", data={"csrf": t, "investimento_eur": "4500", "data_investimento": "2026-06-15",
                                "iva_reduzido": "6", "iva_normal": "23", "limite_iva_kwh_30dias": "200"})
    d = c.get("/api/dados").get_json()["definicoes"]
    assert float(d["investimento_eur"]) == 4500 and float(d["iva_reduzido"]) == 0.06
    assert d["data_investimento"] == "2026-06-15" and d["primeiro_mes"] == "2026-06"


def test_sincronizacao_deye_substitui_meses_iniciais(cliente):
    c = cliente
    entrar(c)
    t = csrf(c)
    FalsaDeye.pedidos.clear()
    r = c.post("/sincronizar", data={"csrf": t}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Atualizado da Deye: set 2026, out 2026." in html and "à mão" not in html
    caminho, corpo, auth = FalsaDeye.pedidos[0]
    assert caminho == "/v1.0/account/token?appId=id" and corpo["appSecret"] == "s"
    assert "password" in corpo and corpo["password"] != "pw"
    assert FalsaDeye.pedidos[-1][2] == "Bearer abc"

    meses = {m["mes"]: m for m in c.get("/api/dados").get_json()["meses"]}
    assert meses["2026-09"]["rede_kwh"] == 40  # setembro vinha dos dados iniciais: passa a vir da Deye
    assert meses["2026-07"]["rede_kwh"] == 15.79  # a Deye não devolveu julho: fica como estava
    assert meses["2026-10"]["rede_kwh"] == 15 and meses["2026-10"]["injetado_kwh"] == 60
    assert meses["2026-10"]["direto_kwh"] == 110 - 15 - 40


def test_login_errado_na_deye(cliente, monkeypatch):
    monkeypatch.setenv("DEYE_EMAIL", "outro@x.pt")
    c = cliente
    entrar(c)
    r = c.post("/sincronizar", data={"csrf": csrf(c)}, follow_redirects=True)
    assert "auth failed" in r.get_data(as_text=True)


def test_cron_da_vercel(cliente):
    c = cliente
    assert c.get("/api/cron/sincronizar").status_code == 401
    assert c.get("/api/cron/sincronizar", headers={"Authorization": "Bearer errado"}).status_code == 401
    r = c.get("/api/cron/sincronizar", headers={"Authorization": "Bearer cron-segredo-123456"})
    assert r.status_code == 200 and r.get_json()["ok"]
    entrar(c)
    meses = {m["mes"]: m for m in c.get("/api/dados").get_json()["meses"]}
    assert meses["2026-10"]["rede_kwh"] == 15


def test_estaticos(cliente):
    c = cliente
    assert c.get("/static/estilo.css").status_code == 200
    assert c.get("/static/chart.umd.min.js").status_code == 200


def test_vercel_mostra_o_que_falta(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL", "1")
    for k in ("SECRET_KEY", "DATABASE_URL", "POSTGRES_URL", "APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import create_app
    r = create_app().test_client().get("/")
    html = r.get_data(as_text=True)
    assert r.status_code == 503 and "DATABASE_URL" in html and "SECRET_KEY" not in html


def test_bd_com_prefixo(monkeypatch):
    from app import db
    for k in ("DATABASE_URL", "POSTGRES_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STORAGE_DATABASE_URL_UNPOOLED", "postgresql://x@h/unpooled")
    monkeypatch.setenv("STORAGE_DATABASE_URL", "postgresql://x@h/db")
    assert db.url_postgres() == "postgresql://x@h/db"


def test_erro_ao_arrancar_nao_mostra_senha(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:SEGREDO@127.0.0.1:1/nada")
    from app import create_app
    html = create_app().test_client().get("/").get_data(as_text=True)
    assert "não conseguiu arrancar" in html and "SEGREDO" not in html


def test_diario_so_le_mes_atual_e_anterior(cliente):
    c = cliente
    entrar(c)
    t = csrf(c)
    c.post("/sincronizar", data={"csrf": t})  # 1.ª vez: histórico todo
    FalsaDeye.pedidos.clear()
    c.post("/sincronizar", data={"csrf": t})  # dia seguinte: só os 2 últimos meses
    hist = [corpo for caminho, corpo, _ in FalsaDeye.pedidos if caminho.endswith("/station/history") and corpo["granularity"] == 3]
    from app.calc import somar_meses
    from datetime import date
    atual = date.today().strftime("%Y-%m")
    assert hist and hist[0]["startAt"] == max("2026-07", somar_meses(atual, -1)) and hist[0]["endAt"] == atual
    FalsaDeye.pedidos.clear()
    c.post("/sincronizar", data={"csrf": t, "completo": "1"})  # botão "Reler todo o histórico"
    hist = [corpo for caminho, corpo, _ in FalsaDeye.pedidos if caminho.endswith("/station/history") and corpo["granularity"] == 3]
    assert hist[0]["startAt"] == "2026-07"


def test_testar_ligacao(cliente):
    c = cliente
    entrar(c)
    html = c.post("/definicoes/testar-deye", data={"csrf": csrf(c)}).get_data(as_text=True)
    assert "Ligação a funcionar" in html and "Casa" in html


def test_sem_variaveis_cria_senha_e_pede_as_da_deye(cliente, monkeypatch):
    c = cliente
    for k in ("APP_PASSWORD", "DEYE_APP_ID", "DEYE_APP_SECRET", "DEYE_EMAIL", "DEYE_PASSWORD", "CRON_SECRET"):
        monkeypatch.delenv(k, raising=False)
    # 1.ª visita: pede para criar a palavra-passe
    r = c.get("/")
    assert r.status_code == 302 and "/criar-senha" in r.headers["Location"]
    assert "As duas" in c.post("/criar-senha", data={"senha": "abcdefgh", "senha2": "outra123"}).get_data(as_text=True)
    assert c.post("/criar-senha", data={"senha": "abcdefgh", "senha2": "abcdefgh"}).status_code == 302
    assert c.get("/").status_code == 200
    # depois de criada, já não se pode criar outra
    assert c.post("/criar-senha", data={"senha": "zzzzzzzz", "senha2": "zzzzzzzz"}).status_code == 302
    c.post("/logout", data={"csrf": csrf(c)})
    assert c.post("/login", data={"senha": "zzzzzzzz"}).status_code == 200  # errada
    assert c.post("/login", data={"senha": "abcdefgh"}).status_code == 302

    # sem as variáveis DEYE_* não há ligação, e a página diz quais faltam (não há formulário)
    html = c.get("/definicoes").get_data(as_text=True)
    assert "DEYE_APP_SECRET" in html and 'name="app_secret"' not in html
    t = csrf(c, "/definicoes")
    assert "variáveis DEYE_*" in c.post("/sincronizar", data={"csrf": t}, follow_redirects=True).get_data(as_text=True)
    for k, v in {"DEYE_APP_ID": "id", "DEYE_APP_SECRET": "s", "DEYE_EMAIL": "eu@x.pt", "DEYE_PASSWORD": "pw"}.items():
        monkeypatch.setenv(k, v)
    dados = json.dumps(c.get("/api/dados").get_json())
    assert "senha_hash" not in dados and "secret_key" not in dados
    r = c.post("/sincronizar", data={"csrf": t}, follow_redirects=True)
    assert "Atualizado da Deye" in r.get_data(as_text=True)

    # cron da Vercel sem CRON_SECRET: só com o user-agent do cron, e no máximo de 6 em 6 horas
    assert c.get("/api/cron/sincronizar").status_code == 401
    r = c.get("/api/cron/sincronizar", headers={"User-Agent": "vercel-cron/1.0"})
    assert r.status_code == 200 and "há pouco" in r.get_json()["mensagem"]



# ---------- faturas da MEO Energia ----------
DADOS = os.path.join(os.path.dirname(__file__), "dados")


def texto(nome):
    return open(os.path.join(DADOS, nome), encoding="utf-8").read()


def test_ler_fatura_meo():
    from app.faturas_meo import ler
    a = ler(texto("meo_2026-08.txt"))
    assert (a["inicio"], a["fim"], a["dias"]) == ("2026-07-09", "2026-08-20", 43)
    assert a["numero"] == "FA ME26/1 885 673" and a["emitida"] == "2026-08-27"
    assert (a["kwh"], a["preco_kwh"], a["potencia_eur_dia"]) == (63.0, 0.1499, 0.47)
    assert (a["energia_eur"], a["potencia_eur"], a["cav_eur"], a["iec_eur"], a["dgeg_eur"]) == (9.44, 20.21, 4.03, 0.06, 0.07)
    assert (a["iva_eur"], a["total_eur"]) == (5.49, 39.30) and a["confere"]
    assert a["extra_vs_regulado_eur"] == 5.99 and a["leitura"] == "real" and a["kwh_vazio"] == 26
    b = ler(texto("meo_2026-09.txt"))
    assert (b["inicio"], b["fim"], b["kwh"], b["total_eur"], b["kwh_vazio"]) == ("2026-08-21", "2026-09-20", 54.0, 29.72, 40)


def test_refazer_fatura_da_o_total_real():
    from app.calc import Impostos, calcular_periodo
    from app.faturas_meo import ler
    for nome in ("meo_2026-08.txt", "meo_2026-09.txt"):
        f = ler(texto(nome))
        r = calcular_periodo(f, {"consumo_kwh": f["kwh"], "rede_kwh": f["kwh"], "injetado_kwh": 0, "fonte": "x"}, None, Impostos())
        assert r["fatura_sem"] == f["total_eur"]  # mesmo consumo → mesma fatura


def test_faturas_repartidas_pelos_meses():
    from app.calc import faturas_por_mes
    from app.faturas_meo import ler
    m = faturas_por_mes([ler(texto("meo_2026-08.txt")), ler(texto("meo_2026-09.txt"))])
    assert m == {"2026-08": round(39.30 * 20 / 43 + 29.72 * 11 / 31, 2)}  # só agosto fica coberto inteiro


def test_carregar_pdf_e_email(cliente, monkeypatch):
    import base64
    from app import faturas_meo
    textos = {b"PDF-AGO": texto("meo_2026-08.txt"), b"PDF-SET": texto("meo_2026-09.txt")}
    monkeypatch.setattr(faturas_meo, "texto_do_pdf", lambda dados, senha=None: textos[dados])
    c = cliente
    entrar(c)
    t = csrf(c)
    c.post("/sincronizar", data={"csrf": t, "completo": "1"})  # dados diários da Deye simulada
    import io
    # por email: endereço secreto
    c.post("/definicoes/email", data={"csrf": t, "acao": "novo"})
    from app import db
    token = db.segredo("email_token")
    assert c.post("/api/email/faturas/errado", json={}).status_code == 404
    r = c.post(f"/api/email/faturas/{token}", data={"subject": "Fatura", "anexo": (io.BytesIO(b"PDF-AGO"), "agosto.pdf")},
               content_type="multipart/form-data")
    assert "FA ME26/1 885 673" in r.get_json()["faturas"][0]
    html = c.get("/faturas").get_data(as_text=True)
    assert "FA ME26/1 885 673" in html and "39,30 €" in html and "Poupaste" in html
    assert "mercado regulado" in html and "bi-horária" not in html  # 26 de 63 kWh em vazio: menos de metade

    corpo = {"headers": {"subject": "A sua fatura MEO Energia", "from": "faturas@meoenergia.pt"}, "plain": "Olá",
             "attachments": [{"file_name": "fatura.pdf", "content_type": "application/pdf",
                              "content": base64.b64encode(b"PDF-SET").decode()}]}
    r = c.post(f"/api/email/faturas/{token}", json=corpo)
    assert r.status_code == 200 and "FA ME26/2 131 663" in r.get_json()["faturas"][0]
    # email sem PDF (ex.: código de confirmação do Gmail) fica visível nas definições
    c.post(f"/api/email/faturas/{token}", json={"headers": {"subject": "Gmail Forwarding Confirmation"}, "plain": "Código 123456"})
    html = c.get("/definicoes").get_data(as_text=True)
    assert "Gmail Forwarding Confirmation" in html and "123456" in html
    html = c.get("/faturas").get_data(as_text=True)
    assert "FA ME26/2 131 663" in html and "bi-horária" in html  # 40 de 54 kWh em vazio
    # com dados diários: 31 dias × 10 kWh em casa
    assert "310 kWh consumidos em casa" in html
    # comparação da rede sempre visível: 54 kWh na fatura, 31 dias × 2 kWh = 62 kWh na Deye (+15 %, amarelo)
    assert "fatura <b>54 kWh</b> · Deye <b>62 kWh</b>" in html
    assert re.search(r'etiqueta sol"[^>]*>\s*\+15 %', html)



def test_reler_historico_depois_de_testar_ligacao(cliente):
    c = cliente
    entrar(c)
    t = csrf(c)
    c.post("/definicoes/testar-deye", data={"csrf": t})
    r = c.post("/sincronizar", data={"csrf": t, "completo": "1"},
               headers={"Referer": "https://x.vercel.app/definicoes/testar-deye"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/definicoes")
    assert c.get("/definicoes/testar-deye").status_code == 302  # abrir o endereço diretamente já não dá 405
    assert c.get("/sincronizar").status_code == 302


# ---------- autofaturas de venda ----------
def test_ler_autofatura_meo():
    from app.faturas_meo import ler
    a = ler(texto("meo_autofatura_2026-09.txt"))
    assert a["tipo"] == "venda" and a["numero"] == "FT AFAT.0ODA0/1" and a["emitida"] == "2026-10-07"
    assert (a["inicio"], a["fim"], a["dias"]) == ("2026-07-21", "2026-09-30", 72)
    assert (a["kwh"], a["preco_kwh"], a["total_eur"]) == (404.21, 0.06, 24.25) and a["confere"]
    assert ler(texto("meo_2026-09.txt"))["tipo"] == "compra"


@pytest.mark.parametrize("linha", [
    # cada coluna da tabela numa linha
    "Excedente energia produzida\n21/07/2026\n30/09/2026\n404,21 kWh\n€ 0,06\n€ 24,25\n0 %",
    # números antes das datas, com espaços que não quebram
    "Excedente energia produzida\n404,21\u00a0kWh €\u00a00,06 €\u00a024,25 0 %\n21/07/2026 30/09/2026",
    # o valor com o € depois
    "Excedente energia produzida 21/07/2026 30/09/2026 404,21 kWh 0,06 € 24,25 € 0 %",
    # tudo por ordem, como aparece no PDF aberto
    "Excedente energia produzida 21/07/2026 30/09/2026 404,21 kWh € 0,06 € 24,25 0 %",
])
def test_ler_autofatura_com_colunas_baralhadas(linha):
    from app.faturas_meo import ler
    real = "0 %Excedente energia produzida   € 0,06 € 24,25 404,21 kWh21/07/2026 30/09/2026"  # como o pypdf a tira
    t = texto("meo_autofatura_2026-09.txt")
    assert real in t
    a = ler(t.replace(real, linha))
    assert (a["inicio"], a["fim"], a["kwh"], a["preco_kwh"], a["total_eur"]) == ("2026-07-21", "2026-09-30", 404.21, 0.06, 24.25)


def test_autofatura_ilegivel_mostra_o_texto():
    from app.faturas_meo import FaturaIlegivel, ler
    t = re.sub(r"\d+,\d+ kWh", "", texto("meo_autofatura_2026-09.txt"))
    with pytest.raises(FaturaIlegivel, match="Texto lido: «Excedente energia produzida"):
        ler(t)


def test_vendas_por_mes():
    from app.calc import vendas_por_mes
    af = [{"inicio": "2026-07-21", "fim": "2026-09-30", "total_eur": 24.25}]
    # sem dados diários: por igual pelos 72 dias
    v = vendas_por_mes(af, {})
    assert v["2026-07"] == {"eur": round(24.25 * 11 / 72, 2), "fracao": round(11 / 31, 4)}
    assert v["2026-08"]["fracao"] == 1 and v["2026-09"]["eur"] == round(24.25 * 30 / 72, 2)
    # com dados diários: pela energia injetada em cada dia (agosto com o dobro do sol de julho e setembro)
    from datetime import date, timedelta
    dias, d = {}, date(2026, 7, 1)
    while d <= date(2026, 9, 30):
        dias[d.isoformat()] = {"injetado_kwh": 10 if d.month == 8 else 5}
        d += timedelta(days=1)
    v = vendas_por_mes(af, dias)
    total = 11 * 5 + 31 * 10 + 30 * 5
    assert v["2026-08"]["eur"] == round(24.25 * 310 / total, 2)
    assert v["2026-07"]["fracao"] == round(55 / 155, 4)
    # na poupança: julho só tem 11 dias cobertos, o resto continua estimado
    res = calcular(linhas(), [MEO, VENDA], {}, Impostos(), date(2026, 10, 12), v)
    jul, ago = res[0], res[1]
    assert ago["venda_eur"] == v["2026-08"]["eur"] and ago["venda_real"] == "total"
    assert jul["venda_real"] == "parcial"
    assert jul["venda_eur"] == round(v["2026-07"]["eur"] + 40.59 * 0.06 * (1 - v["2026-07"]["fracao"]), 2)


def test_autofatura_por_email(cliente, monkeypatch):
    import base64
    from app import db, faturas_meo
    monkeypatch.setattr(faturas_meo, "texto_do_pdf", lambda dados, senha=None: texto("meo_autofatura_2026-09.txt"))
    c = cliente
    entrar(c)
    t = csrf(c)
    c.post("/sincronizar", data={"csrf": t, "completo": "1"})
    c.post("/definicoes/email", data={"csrf": t, "acao": "novo"})
    token = db.segredo("email_token")
    corpo = {"headers": {"subject": "Autofatura"}, "attachments": [
        {"file_name": "auto.pdf", "content_type": "application/pdf", "content": base64.b64encode(b"x").decode()}]}
    r = c.post(f"/api/email/faturas/{token}", json=corpo)
    assert "Autofatura FT AFAT.0ODA0/1" in r.get_json()["faturas"][0] and "404 kWh injetados" in r.get_json()["faturas"][0]

    # fica no separador Venda, não no de Consumo
    assert "FT AFAT.0ODA0/1" not in c.get("/faturas").get_data(as_text=True)
    html = c.get("/faturas?tipo=venda").get_data(as_text=True)
    assert "24,25 €" in html and "FT AFAT.0ODA0/1" in html
    # Deye simulada: 7 kWh injetados × 72 dias = 504 kWh, +25 % face à autofatura (vermelho)
    assert "autofatura <b>404 kWh</b> · Deye <b>504 kWh</b>" in html
    assert re.search(r'etiqueta perigo"[^>]*>\s*\+25 %', html)

    # agosto (todo coberto) passa a usar o valor real da autofatura
    meses = {m["mes"]: m for m in c.get("/api/dados").get_json()["meses"]}
    assert meses["2026-08"]["venda_real"] == "total" and meses["2026-08"]["venda_eur"] == round(24.25 * 31 / 72, 2)

    c.post("/faturas/FT AFAT.0ODA0/1/apagar", data={"csrf": t})
    assert "FT AFAT.0ODA0/1" not in c.get("/faturas?tipo=venda").get_data(as_text=True)


def test_balanco_producao_consumo():
    from app import _balanco
    b = _balanco("2026-09", {"consumo_kwh": 280.99, "rede_kwh": 43.8, "injetado_kwh": 219.89, "producao_kwh": 470})
    assert b == {"p": "2026-09", "producao": 470, "solar_casa": 237.19, "injetado": 219.89, "rede": 43.8, "consumo": 280.99}
    # sem produção registada (meses escritos à mão): produção = solar consumido + injetado
    assert _balanco("2026-07", {"consumo_kwh": 216.37, "rede_kwh": 15.79, "injetado_kwh": 40.59})["producao"] == 241.17


def test_pagina_energia_tem_grafico(cliente):
    c = cliente
    entrar(c)
    html = c.get("/meses").get_data(as_text=True)
    assert 'id="g-balanco"' in html and '"solar_casa"' in html
