"""Base de dados: SQLite num ficheiro (servidor próprio / Docker) ou Postgres (Vercel, via DATABASE_URL).

O resto da app escreve SQL com marcadores `?`; aqui são convertidos para o Postgres quando é o caso.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def url_postgres() -> str | None:
    """A ligação ao Postgres. A Vercel/Neon pode dar-lhe vários nomes (com ou sem prefixo)."""
    preferidas = ("DATABASE_URL", "POSTGRES_URL", "NEON_DATABASE_URL")
    candidatas = [k for k in preferidas if k in os.environ] + sorted(
        k for k in os.environ
        if k.endswith(("DATABASE_URL", "POSTGRES_URL")) and "UNPOOLED" not in k and "NON_POOLING" not in k
        and k not in preferidas)
    for k in candidatas:
        v = os.environ.get(k, "")
        if v.startswith(("postgres://", "postgresql://")):
            return v
    return None


def usa_postgres() -> bool:
    return url_postgres() is not None


def agora() -> str:
    """Data e hora UTC em texto, igual nos dois motores."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _esquema(pg: bool) -> list[str]:
    idauto = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if pg else "REAL"
    return [
        f"""CREATE TABLE IF NOT EXISTS contratos (
            id {idauto},
            tipo TEXT NOT NULL CHECK (tipo IN ('compra', 'venda')),
            empresa TEXT NOT NULL,
            desde TEXT NOT NULL,
            preco_kwh {real} NOT NULL,
            potencia_eur_dia {real} NOT NULL DEFAULT 0,
            iva_potencia {real},
            notas TEXT DEFAULT '')""",
        f"""CREATE TABLE IF NOT EXISTS faturas (
            mes TEXT PRIMARY KEY,
            total_eur {real} NOT NULL,
            notas TEXT DEFAULT '')""",
        f"""CREATE TABLE IF NOT EXISTS meses (
            mes TEXT PRIMARY KEY,
            consumo_kwh {real}, rede_kwh {real}, injetado_kwh {real},
            producao_kwh {real}, bateria_kwh {real}, carga_bateria_kwh {real},
            origem TEXT NOT NULL DEFAULT 'deye',
            atualizado TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS definicoes (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS sincronizacoes (
            id {idauto},
            quando TEXT NOT NULL,
            ok INTEGER NOT NULL,
            mensagem TEXT)""",
    ]


DEFINICOES_INICIAIS = {
    "investimento_eur": "3979.32",
    "primeiro_mes": "2026-07",
    "iva_reduzido": "0.06",
    "iva_normal": "0.23",
    "limite_iva_kwh_30dias": "200",
}


class Ligacao:
    """Envolve a ligação para dar a mesma interface aos dois motores: execute() devolve uma lista de dicts."""

    def __init__(self, con, pg: bool):
        self.con, self.pg = con, pg

    def execute(self, sql: str, params: tuple | list = ()) -> list[dict]:
        if self.pg:
            sql = sql.replace("?", "%s")
        cur = self.con.cursor()
        cur.execute(sql, tuple(params))
        if cur.description is None:
            return []
        linhas = cur.fetchall()
        return [dict(r) for r in linhas]

    def executemany(self, sql: str, seq) -> None:
        for p in seq:
            self.execute(sql, p)

    def um(self, sql: str, params=()) -> dict | None:
        r = self.execute(sql, params)
        return r[0] if r else None


def caminho_sqlite() -> str:
    pasta = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
    os.makedirs(pasta, exist_ok=True)
    return os.path.join(pasta, "poupanca.db")


@contextmanager
def ligar():
    url = url_postgres()
    if url:
        import psycopg
        from psycopg.rows import dict_row
        con = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
        pg = True
    else:
        con = sqlite3.connect(caminho_sqlite(), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        pg = False
    try:
        yield Ligacao(con, pg)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def iniciar() -> None:
    with ligar() as con:
        for sql in _esquema(con.pg):
            con.execute(sql)
        novo = con.um("SELECT COUNT(*) AS n FROM definicoes")["n"] == 0
        for k, v in DEFINICOES_INICIAIS.items():
            con.execute("INSERT INTO definicoes (chave, valor) VALUES (?, ?) ON CONFLICT (chave) DO NOTHING", (k, v))
        if novo and os.environ.get("SEM_DADOS_INICIAIS") != "1":
            _dados_iniciais(con)


def _dados_iniciais(con: Ligacao) -> None:
    """Os teus contratos e meses de julho a setembro de 2026, para não começares do zero."""
    con.executemany(
        "INSERT INTO contratos (tipo, empresa, desde, preco_kwh, potencia_eur_dia, iva_potencia, notas) VALUES (?,?,?,?,?,?,?)",
        [
            ("compra", "MEO Energia", "2026-07-01", 0.1499, 0.47, 0.23, "Simples 5,75 kVA — preços sem IVA"),
            ("venda", "Comprador do excedente", "2026-07-01", 0.06, 0, None, "Venda da energia injetada"),
        ],
    )
    # (mês, rede, painéis direto, bateria, injetado) — valores da app Deye Cloud
    for mes, rede, direto, bat, inj in [
        ("2026-07", 15.79, 101.79, 98.79, 40.59),
        ("2026-08", 48.19, 81.70, 105.89, 187.49),
        ("2026-09", 43.80, 110.79, 126.40, 219.89),
    ]:
        con.execute(
            "INSERT INTO meses (mes, consumo_kwh, rede_kwh, injetado_kwh, bateria_kwh, origem, atualizado) "
            "VALUES (?,?,?,?,?,'manual',?)",
            (mes, round(rede + direto + bat, 2), rede, inj, bat, agora()),
        )


def definicoes() -> dict[str, str]:
    with ligar() as con:
        return {r["chave"]: r["valor"] for r in con.execute("SELECT chave, valor FROM definicoes")}


def guardar_definicao(chave: str, valor: str) -> None:
    with ligar() as con:
        con.execute("INSERT INTO definicoes (chave, valor) VALUES (?, ?) "
                    "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor", (chave, valor))
