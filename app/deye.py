"""Cliente mínimo da API oficial Deye Cloud (developer.deyecloud.com)."""
from __future__ import annotations

import hashlib
from datetime import date

import requests

from .calc import somar_meses


class DeyeErro(Exception):
    pass


class DeyeCloud:
    def __init__(self, base_url: str, app_id: str, app_secret: str, login: str, password: str,
                 station_id: str | None = None, timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.app_id, self.app_secret = app_id, app_secret
        self.login, self.password = login.strip(), password
        self.station_id = station_id
        self.timeout = timeout
        self.token: str | None = None

    def _post(self, caminho: str, corpo: dict, auth: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        try:
            r = requests.post(self.base + caminho, json=corpo, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise DeyeErro(f"Sem ligação à Deye Cloud: {e}") from e
        if r.status_code >= 400:
            raise DeyeErro(f"A Deye respondeu {r.status_code}: {r.text[:300]}")
        j = r.json()
        if j.get("success") is False:
            raise DeyeErro(f"A Deye recusou o pedido {caminho}: {j.get('msg') or j}")
        return j

    def autenticar(self) -> None:
        corpo = {
            "appSecret": self.app_secret,
            "password": hashlib.sha256(self.password.encode("utf-8")).hexdigest(),
        }
        corpo["email" if "@" in self.login else "username"] = self.login
        j = self._post(f"/account/token?appId={self.app_id}", corpo, auth=False)
        token = j.get("accessToken")
        if not token:
            raise DeyeErro(f"A Deye não devolveu token: {j.get('msg') or j}")
        self.token = str(token).removeprefix("Bearer ").strip()

    def estacoes(self) -> list[dict]:
        """As instalações (estações) da conta: [{id, nome}]."""
        if not self.token:
            self.autenticar()
        j = self._post("/station/list", {"page": 1, "size": 20})
        return [{"id": str(s.get("id")), "nome": s.get("name") or s.get("stationName") or f"Instalação {s.get('id')}"}
                for s in (j.get("stationList") or [])]

    def estacao(self) -> str:
        if self.station_id:
            return self.station_id
        lista = self.estacoes()
        if not lista:
            raise DeyeErro("A conta não tem nenhuma instalação na Deye Cloud.")
        self.station_id = lista[0]["id"]
        return self.station_id

    def historico_mensal(self, inicio: str, fim: str | None = None) -> list[dict]:
        """Totais por mês entre inicio e fim (AAAA-MM). A API aceita até 12 meses por pedido."""
        if not self.token:
            self.autenticar()
        estacao = self.estacao()
        fim = fim or date.today().strftime("%Y-%m")
        meses, a = [], inicio
        while a <= fim:
            b = min(somar_meses(a, 11), fim)
            j = self._post("/station/history", {
                "stationId": int(estacao), "granularity": 3, "startAt": a, "endAt": b,
            })
            for it in j.get("stationDataItems") or []:
                meses.append({
                    "mes": f"{int(it['year']):04d}-{int(it['month']):02d}",
                    "consumo_kwh": _f(it.get("consumptionValue")),
                    "rede_kwh": _f(it.get("purchaseValue")),
                    "injetado_kwh": _f(it.get("gridValue")),
                    "producao_kwh": _f(it.get("generationValue")),
                    "bateria_kwh": _f(it.get("dischargeValue")),
                    "carga_bateria_kwh": _f(it.get("chargeValue")),
                })
            a = somar_meses(b, 1)
        return meses


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
