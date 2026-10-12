# ☀️ Poupança solar

Aplicação web para acompanhar quanto poupas com os painéis solares e quando chegas ao break-even.

- Vai buscar os dados à **Deye Cloud** sozinha, uma vez por dia (API oficial).
- **Contratos** de compra e de venda de energia, com preço do kWh, potência e data de início, editáveis na página.
- **Faturas** reais por mês (opcional), para comparar com a estimativa.
- Painel com poupança acumulada, % recuperada, break-even previsto, autossuficiência e gráficos.
- Login com palavra-passe. Os dados ficam em Postgres (na Vercel) ou num ficheiro SQLite (no Docker).

Corre na **Vercel** (com Postgres Neon) ou em qualquer servidor com **Docker** (com SQLite).

Já vem com os teus contratos (MEO e venda do excedente), o investimento (3 979,32 €) e os meses de julho a setembro de 2026.

---

## 1. Credenciais da Deye Cloud (uma vez)

1. Entra em **https://developer.deyecloud.com** com o mesmo email da app Deye Cloud.
2. Cria uma aplicação (*Application*). Guarda o **App ID** e o **App Secret**.

Sem isto a app funciona na mesma: escreves os meses à mão na página **Energia**.

## 2. Publicar na Vercel (recomendado, grátis)

Na Vercel a app corre como função serverless: os dados ficam numa base de dados **Postgres (Neon)** e a sincronização diária é um **cron job** da Vercel.

1. **Código no GitHub** — o repositório `poupanca-solar` (privado).
2. Em **vercel.com → Add New… → Project**, importa esse repositório. A Vercel reconhece o Flask sozinha; não mudes nada no ecrã de build.
3. **Base de dados** — no projeto, separador **Storage → Create Database → Neon (Postgres)**, plano grátis, região Frankfurt, e liga-a ao projeto. Isto cria a variável `DATABASE_URL`.
4. **Variáveis** — em **Settings → Environment Variables** adiciona:

   | Nome | Valor |
   |---|---|
   | `APP_PASSWORD` | a palavra-passe para entrares na página |
   | `SECRET_KEY` | texto aleatório longo (ex.: gera em 1password.com/password-generator, 40+ caracteres) |
   | `CRON_SECRET` | outro texto aleatório (16+ caracteres) |
   | `DEYE_APP_ID`, `DEYE_APP_SECRET` | da tua aplicação em developer.deyecloud.com |
   | `DEYE_EMAIL`, `DEYE_PASSWORD` | o login da app Deye Cloud |

5. **Deployments → Redeploy** para as variáveis entrarem. Abre o endereço `…vercel.app` e entra.

Notas da Vercel:
- O cron (`vercel.json`) corre todos os dias às 06:00 UTC (no plano grátis, a qualquer minuto dessa hora) e relê o mês atual e o anterior, porque a Deye pode acrescentar dados atrasados nos primeiros dias do mês. Em **Definições** tens **Testar ligação** e **Reler todo o histórico**.
- Cada `git push` para o repositório publica uma versão nova automaticamente.
- Na primeira visita a base de dados é criada e preenchida com os teus dados iniciais.

## 2b. Ou num servidor teu com Docker

Qualquer máquina com Docker sempre ligada serve:

| Opção | Custo | Notas |
|---|---|---|
| **Raspberry Pi / mini-PC em casa** | só a luz | Fica na tua rede; para aceder de fora usa Tailscale ou Cloudflare Tunnel (grátis). |
| **VPS** (Hetzner, Contabo, OVH…) | ~4–5 €/mês | Acessível de qualquer lado. Põe HTTPS à frente (Caddy, ver abaixo). |
| **NAS** (Synology, QNAP, Unraid) | — | Se tiver Docker/Container Manager. |

### Instalar com Docker

```bash
# copia a pasta para o servidor e entra nela
cd poupanca-solar
cp .env.example .env
nano .env            # preenche APP_PASSWORD, SECRET_KEY e as quatro DEYE_*
docker compose up -d --build
```

Abre `http://IP-DO-SERVIDOR:8000` e entra com a `APP_PASSWORD`.

- Na página **Definições** confirmas que a ligação à Deye ficou *configurada*.
- O botão **↻ Atualizar da Deye** sincroniza logo; depois corre sozinho todos os dias às 6h.
- Ver o que se passa: `docker compose logs -f`
- Atualizar a app depois de mudar o código: `docker compose up -d --build`

#### Sem Docker (Python 3.10+)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a && . ./.env && set +a
gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 wsgi:app
```

Usa sempre **um só processo** (`-w 1`): a sincronização diária corre dentro dele.

### HTTPS (se estiver na internet)

Com um domínio a apontar para o servidor, o [Caddy](https://caddyserver.com) trata do certificado sozinho. Um `Caddyfile` mínimo:

```
solar.teudominio.pt {
    reverse_proxy localhost:8000
}
```

Depois põe `COOKIE_SEGURO=1` no `.env` e reinicia. Não deixes a porta 8000 aberta para a internet, só a 443.

## Faturas da MEO Energia

As faturas vão de um dia a outro (ex.: 21/08 a 20/09), não de mês a mês. Para cada fatura, a app:

- lê o PDF: período, kWh, preço do kWh, potência, CAV, IEC, DGEG, IVA e total (sem guardar nome, morada, NIF ou IBAN);
- junta os dados **diários** da Deye desse período exato e refaz a fatura como se toda a energia viesse da rede;
- avisa se o preço mudou face ao contrato (e cria o contrato novo com um clique), se a leitura foi estimada, quanto pagarias no mercado regulado e se compensaria uma tarifa bi-horária;
- reparte o total pelos meses, para o painel mensal usar a fatura real quando o mês está todo coberto.

Para as receber **por email**: em Definições → Faturas por email, liga a receção e segue os passos (um serviço como o CloudMailin transforma o email num pedido para a app; no Gmail, um filtro reencaminha os emails da MEO).

## 3. Uso no dia a dia

- **Mudaste de empresa ou de preço?** Em **Contratos**, adiciona um contrato novo com a data de início. Não apagues o antigo: os meses passados continuam com o preço que tinham.
- **Chegou a fatura?** Larga o PDF em **Faturas** (ou deixa-a chegar por email).
- **Investiste mais** (ex.: outra bateria)? Em **Definições**, atualiza o investimento total.
- **Um mês está errado?** Em **Energia**, escreve-o à mão (fica fixo) ou apaga-o para voltar a vir da Deye.

## Como se calcula

- **Fatura sem painéis** = todo o consumo da casa comprado ao preço do contrato de compra + potência, com IVA.
- **Fatura com painéis** = só a energia comprada à rede + potência, com IVA (ou a fatura real, se a registaste).
- **Poupança** = fatura sem painéis − fatura com painéis + energia injetada × preço do contrato de venda.
- IVA da energia: taxa reduzida nos primeiros 200 kWh por 30 dias, normal no resto (configurável).
- **Break-even previsto** = o que falta ÷ poupança média dos meses fechados. O mês em curso não entra na média.
- Dados da Deye usados: consumo (`consumptionValue`), rede (`purchaseValue`), injetado (`gridValue`), descarga da bateria (`dischargeValue`).

## Cópias de segurança

- **Vercel/Neon:** o Neon guarda histórico e permite restaurar; para uma cópia tua, usa `pg_dump` com o `DATABASE_URL`.
- **Docker:** tudo está em `data/poupanca.db`. Copia esse ficheiro de vez em quando.

## Problemas

- **"A Deye recusou o pedido … auth"** — confirma email, palavra-passe e App Secret no `.env`.
- **Conta fora da Europa** — define `DEYE_BASE_URL` (ex.: `https://us1-developer.deyecloud.com/v1.0`).
- **Mais de uma instalação** — define `DEYE_STATION_ID`.
- **Os números da Deye não batem com a app** — diz qual o campo; os nomes acima podem precisar de ajuste para o teu inversor.

## Testes

```bash
pip install pytest && python -m pytest -q
# para testar também com Postgres:
TEST_PG_URL=postgresql://utilizador@localhost/postgres python -m pytest -q
```

Incluem os cálculos (conferidos à mão) e uma Deye Cloud simulada.
