# Alocador tático — Módulo 4 do Ferramentas Financeiras

Reescrita do antigo *Allocation Bot v7* (Google Apps Script numa planilha) para
viver **dentro deste repo**, como mais um módulo do site, reaproveitando o
pipeline (GitHub Actions) e os dados que já são gerados.

---

## 1. Plataforma — por que aqui, e não na planilha

Você já tem Python + GitHub Actions + GitHub Pages rodando (breadth, fundos,
simulador). Era redundante manter um bot paralelo em Apps Script. O Alocador
agora é o **módulo 4**:

```
scripts/allocator.py     motor (server-side): lê os JSONs do pipeline, busca o
                         que falta (P/L, Selic, IPCA, AUM) e escreve allocation.json
scripts/email_render.py  e-mail HTML semanal (mesma estética do site)
docs/alocador.html       a página interativa (a "aba" do site) — calcula AO VIVO
docs/allocation.json     snapshot de mercado + defaults (gerado pelo Actions)
config.json              seus dados/constraints p/ o e-mail (gerado pelo botão Exportar)
```

**Divisão de trabalho dado×input** (sua ideia): o que dá para buscar, o pipeline
busca; o que não dá (caixa, carteira total, valor por sleeve, gates, λ, banda) e
qualquer override, **você digita na própria página**. Os seus dados ficam só no
seu navegador (`localStorage`); o botão **Exportar config.json** gera o arquivo
para o e-mail semanal usar os mesmos números.

`docs/alocador.html` e `scripts/allocator.py` compartilham a **mesma matemática**
(testado: dão score e tranche idênticos). A página é a calculadora ao vivo; o
script é só para o e-mail e para deixar um snapshot público no `allocation.json`.

---

## 2. O que mudou em relação ao v7

| Tema | v7 (planilha) | v8 (módulo) |
|---|---|---|
| Plataforma | Apps Script + planilha | repo Python + Pages + Actions |
| Inputs | preenchidos toda semana | auto-fetch + você digita o resto na página |
| Métricas | 11 indicadores redundantes | 4 grupos ~ortogonais, ancorados no ERP real |
| Buffett / CAPE | pontuados | fora do núcleo (CAPE vira multiplicador leve opcional) |
| Ritmo de deploy | tabela de tranches em degraus | gap geométrico até piso dependente da barateza |
| Escopo | só Ártica+Organon | carteira inteira (núcleo, ALOS3, SPXR11, HASH; Nu c/ gate) |
| Risco de fundo | — | monitor de decay (AUM/teto, alfa, concentração) |

### Métricas e pesos (somam 1,00)
- **Valuation 40%** — `erp` 0,22 (earnings yield − CDI real; *a* métrica colada
  no seu hurdle), `pl` 0,10, `dy` 0,08.
- **Drawdown/contrarian 32%** — `ibov_dd` 0,16, `fund_dd` 0,16 (sinal limpo e
  robusto; foi o que dirigiu o backtest).
- **Breadth 18%** — seu composto proprietário 0,12 + `mm200` 0,06.
- **Sentimento 10%** — Fear & Greed (opcional).

Buffett saiu (lento, difícil de automatizar) e CAPE virou multiplicador leve de
regime (±15%), porque eram redundantes com a perna de valuation.

### Ritmo de deploy (o seu foco)
```
piso-alvo = piso_caro − (piso_caro − piso_barato) · (score/10)
tranche   = λ · (caixa% − piso-alvo) · carteira      (≥0; trava em munição mínima)
```
Deploya uma **fração λ do gap** até um piso que cai quando o mercado fica barato.
Aproxima-se do piso geometricamente → **nunca zera munição** e **acelera no
fundo**. Banda **5% (barato) ↔ 20% (caro)**, λ≈0,22.

---

## 3. Backtest (premissa de aporte corrigida)

Rodado nos ciclos reais 2014-16 / 2020 / 2021-22 / 2026 (IBOV+CDI mensais).
**Premissa corrigida:** aporte é valor ~fixo em termos reais (cresce com
inflação), não % da carteira — começa em 3,4%/mês e **encolhe para ~0,3%/mês**
em 13 anos.

| Estratégia | IRR | MaxDD | Sharpe | Caixa mín. | Caixa fim |
|---|---|---|---|---|---|
| Sempre 100% RV | 14,4% | −43% | 1,21 | 0% | 0% |
| Tranche v7 | 14,9% | −37% | 1,43 | 5% | 9% |
| **Engine λ.22 (5/20)** | **14,1%** | **−34%** | **1,43** | **13%** | **18%** |

Conclusões que sobrevivem: prêmio da bolsa sobre CDI de só **~4pp a.a.** no
Brasil (logo munição custa pouco); o engine **nunca esgota munição** (≥13% mesmo
no −45% de 2020 — propriedade estrutural); λ entre 0,15–0,35 é indiferente; banda
5/20 é adequada. O ganho do engine sobre o v7 **não é retorno** — é robustez
(deploy suave, sem degraus, com piso que o "CRISE 50%/sem" do v7 não tinha) e
cobrir a carteira toda.

---

## 4. Setup

1. **Secrets** (repo → Settings → Secrets → Actions): `SMTP_USER` (seu Gmail) e
   `SMTP_PASS` (senha de app do Gmail). Sem isso, o e-mail é pulado (a página
   funciona igual).
2. **config.json** na raiz: gere pelo botão *Exportar config.json* na página, ou
   edite à mão (schema abaixo). Só é usado pelo e-mail.
3. **Workflow**: acrescente o job abaixo ao `.github/workflows/update.yml`
   (depois de `update-breadth`, antes do `deploy`):

```yaml
  alocador:
    runs-on: ubuntu-latest
    needs: [update-breadth]
    if: always() && (needs.update-breadth.result == 'success' || needs.update-breadth.result == 'skipped')
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install requests pandas numpy
      - name: Calcula allocation.json (+ e-mail às segundas)
        env:
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
        run: |
          if [ "$(date -u +%u)" = "1" ]; then python scripts/allocator.py --email
          else python scripts/allocator.py; fi
      - name: Commit allocation.json
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add docs/allocation.json
          git diff --cached --quiet || git commit -m "chore: allocation $(date -u +%Y-%m-%d)"
          git pull --rebase origin main && git push
```
   Lembre de fazer o `deploy` depender também de `alocador` (`needs: [alocador]`).
4. **Link no índice**: adicione um card para `alocador.html` no `docs/index.html`.

> ⚠️ Os fetchers ao vivo (Oceans14 para P/L, BCB SGS para Selic/IPCA, CVM para
> AUM) não puderam ser testados no ambiente onde foram escritos — todos degradam
> com `try/except`. Rode o workflow uma vez (`workflow_dispatch`) e confira o
> `allocation.json`; se o P/L vier vazio, ajuste o seletor em `fetch_pl_ibov()`
> ou use o override na página.

---

## 5. config.json — schema

```json
{
  "caixa": 90000,
  "carteira_total": 600000,
  "lambda_deploy": 0.22,
  "piso_caixa_barato": 0.05,
  "piso_caixa_caro": 0.20,
  "municao_minima": 0.05,
  "pl_override": null,
  "dy_override": 5.7,
  "selic_override": null,
  "ipca_override": null,
  "fear_greed": 4,
  "cape": 9.4,
  "override_sinal": null,
  "sleeves": {
    "Organon": { "atual": 150000, "gate": "aberto" },
    "Artica":  { "atual": 144000, "gate": "aberto" },
    "ROXO34":  { "atual": 72000,  "gate": "fechado" },
    "ALOS3":   { "atual": 30000,  "gate": "aberto" },
    "SPXR11":  { "atual": 0,      "gate": "aberto" },
    "HASH11":  { "atual": 9000,   "gate": "aberto" }
  }
}
```
Alvos (`peso`), caps e gates default ficam em `DEFAULT_CONFIG` no `allocator.py`;
`config.json` sobrescreve o que você quiser.

---

*Leitura do seu material pelo nosso framework — não é recomendação. A decisão é sua.*
