# Módulo 04 — Alocador Tático (v9)

Três baldes (**Caixa · Brasil · S&P**) e **dois sinais de barateza independentes**, cada
mercado dirigido pelo seu próprio sinal. Substitui o v7/v8 e corrige o erro conceitual de
desplegar SPXR11 pelo breadth do IBOV.

## Arquivos

| arquivo | papel |
|---|---|
| `docs/alocador.html` | página (estética idêntica ao site; recalcula client-side) |
| `docs/index.html` | já inclui o card "módulo 04" |
| `scripts/allocator.py` | motor server-side: junta inputs -> `docs/allocation.json` + e-mail |
| `scripts/fetch_breadth_us.py` | breadth do S&P -> `docs/breadth_us.json` |
| `scripts/email_render.py` | HTML do e-mail semanal |
| `config.json` | seus números e parâmetros (raiz do repo) |
| `docs/breadth_us.json` | seed; sobrescrito pelo fetcher no Actions |

## Como funciona

1. **`fetch_breadth_us.py`** tenta as séries oficiais `^S5TW`/`^S5FI`/`^S5TH` via yfinance
   (leve, composição correta, sem survivorship). Se o Yahoo não servir, **cai pro cálculo
   próprio** dos ~500 constituintes. Os dois caminhos aplicam o **mesmo composite**
   (0,15*MA20 + 0,35*MA50 + 0,50*MA200) e os mesmos regimes do breadth do IBOV.
2. **`allocator.py`** lê `breadth.json`, `breadth_us.json`, `data.json`, `ibov_price.json`;
   busca P/L (Oceans14), Selic/IPCA (BCB), P/E+DY (multpl), juro real (FRED `DFII10`),
   F&G (CNN); computa **dois composites** e a **decisão de deploy**; grava
   `docs/allocation.json`. Com `--email`, manda o resumo.
3. **`alocador.html`** carrega `allocation.json`, pré-preenche os campos e **recalcula no
   navegador** (mesma matemática do Python — equivalência verificada). Você edita, clica
   **Aplicar**, e pode **exportar `config.json`**.

### Decisão (resumo da lógica)
- **Piso-alvo de caixa** = interpola entre `caixa_max` (tudo caro) e `caixa_min` (algo barato),
  conforme a **melhor** oportunidade entre os dois mercados.
- **Quanto** = `lambda * (caixa% - piso) * total` (gap geométrico — **nunca zera a munição**).
- **Onde** = atratividade `(quão abaixo do alvo) x gate_barateza(score)`, com
  `gate(s)=clamp((s-4,5)/5,5; 0; 1)`. **Mercado caro é vetado mesmo se sub-alocado.**
- Deploy efetivo = só o que tem destino barato+sub-alocado (o resto fica como munição).

## config.json (schema)

```json
{
  "caixa": 90000, "brasil": 300000, "sp": 60000,
  "alvo_brasil": 0.75, "alvo_sp": 0.25,
  "caixa_min": 0.05, "caixa_max": 0.20, "lambda_deploy": 0.22, "municao_minima": 0.05,
  "email_to": "voce@email.com",
  "pl_br_override": null, "dy_br_override": null, "selic_override": null,
  "ipca_override": null, "fg_br": null,
  "pe_us_override": null, "dy_us_override": null, "real_us_override": null,
  "fg_us_override": null, "override_sinal": null
}
```
Deixe os `*_override` em `null` para usar o fetch ao vivo. `override_sinal: "HOLD"` congela o deploy.

## GitHub Actions — jobs a acrescentar no `update.yml`

```yaml
  breadth-us:
    runs-on: ubuntu-latest
    needs: update-breadth
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: python scripts/fetch_breadth_us.py
      - run: |
          git config user.name  github-actions
          git config user.email actions@github.com
          git add docs/breadth_us.json
          git commit -m "breadth US $(date -u +%F)" || echo "sem mudancas"
          git push

  alocador:
    runs-on: ubuntu-latest
    needs: breadth-us
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: |
          if [ "$(date +%u)" = "1" ]; then python scripts/allocator.py --email; else python scripts/allocator.py; fi
        env:
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
      - run: |
          git config user.name  github-actions
          git config user.email actions@github.com
          git add docs/allocation.json
          git commit -m "alocacao $(date -u +%F)" || echo "sem mudancas"
          git push
```
E faça o job `deploy` depender de `alocador` (`needs: [..., alocador]`).
**Secrets:** `SMTP_USER`, `SMTP_PASS` (senha de app do Gmail).

## Notas honestas
- Nao da pra testar os fetches ao vivo na sandbox (rede limitada); a **logica** foi testada
  por overrides e a equivalencia **JS == Python** foi verificada. Os fetches rodam na internet
  aberta do Actions.
- `^S5TW/^S5FI/^S5TH` no Yahoo e incerto — por isso o fallback de calculo proprio existe.
- Single-names (Nu, ALOS) e cripto **nao** estao na ferramenta: sao discricionarios.

## DD dos fundos — fonte (atualizado)

O DD de Organon+Ártica que entra no composite Brasil vem **da sua planilha de cotas
diárias** (`1PT-cC...`), lida via CSV do gviz:
`https://docs.google.com/spreadsheets/d/<ID>/gviz/tq?tqx=out:csv&sheet=organon` (e `&sheet=artica`).
Aplica a **mesma lógica do seu Apps Script**: ATH sobre `cota_cvm`; cota atual = `cota_cvm`,
com **fallback para `cota_site`** quando a CVM ainda não publicou (e se o site fizer novo topo,
o ATH acompanha). Fallback geral: `data.json` (só CVM, defasado 2-3 d.u.).

**Dois requisitos para o fetch funcionar no Actions (sem login):**
1. A planilha precisa estar **"qualquer pessoa com o link: leitor"** (ou Arquivo → Publicar na web).
   Se ficar restrita, o gviz devolve HTML de login e o parser cai no `data.json`.
2. **Agende o job do alocador depois da sua atualização das 11h–12h** (ex.: 12:30 BRT), para
   ler a planilha já com a cota do dia. Não precisa reconstruir nada — só ler.
