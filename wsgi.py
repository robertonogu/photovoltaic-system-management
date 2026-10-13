import os


def _carregar_env(caminho=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Lê o .env local (se existir) para correr sem Docker. As variáveis já definidas no sistema ganham."""
    if not os.path.exists(caminho):
        return
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


_carregar_env()

from app import create_app  # noqa: E402 — depois de carregar o .env

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
