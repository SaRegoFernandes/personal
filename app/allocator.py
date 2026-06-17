#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 ALOCADOR  v8  —  motor de alocação tático para carteira concentrada BR + global
================================================================================

Reescrita do antigo "Allocation Bot v7" (Google Apps Script) para rodar dentro
do pipeline Python / GitHub Actions / GitHub Pages que já existe neste repo.

O que mudou em relação ao v7 (resumo — ver README_alocador.md para a íntegra):
  1. PLATAFORMA: deixa de ser planilha+Apps Script e passa a ser um script do
     pipeline. Lê os JSONs que o repo já gera (breadth.json, data.json,
     ibov_price.json) e busca só o que falta (P/L, Selic, IPCA). Publica
     docs/allocation.json + dashboard docs/alocador.html + e-mail.
  2. MÉTRICAS: 11 indicadores redundantes -> 4 grupos de fatores ~ortogonais,
     ancorados no ERP real (earnings yield - CDI real), que é a métrica colada
     no SEU hurdle (CDI). Buffett e CAPE saem do núcleo (lentos / difíceis de
     automatizar) e viram override opcional.
  3. RITMO DE DEPLOY: a tabela de tranches grosseira (que no backtest deixava
     53% em caixa por não acompanhar os aportes) vira um mecanismo suave de
     "gap geométrico": a cada período deploya uma fração lambda da distância
     entre o caixa atual e um piso-alvo que depende da barateza do mercado.
     Aproxima-se do piso geometricamente -> NUNCA zera munição e ACELERA quando
     está mais barato. Banda de caixa 5% (barato) a 20% (caro), validada em
     backtest nos ciclos 2014-16 / 2020 / 2021-22 / 2026.
  4. CARTEIRA INTEIRA: não decide mais só Ártica+Organon. Distribui o deploy
     entre os sleeves-alvo (núcleo, ALOS3, SPXR11), roteando para o que está
     mais abaixo do alvo. Nu (ROXO34) entra com "gate" fechado até o 2T26.
  5. MONITOR DE DECAY: usa o PL (AUM) da CVM para alertar Organon perto do teto
     de ~R$1bi, crescimento rápido de AUM e alfa rolante 5-7a — operacionaliza
     o framework de capacity/pessoa-chave.

Tudo é "leitura do material", não recomendação: o script calcula; a decisão é sua.

Fontes de dados (todas com fallback p/ não quebrar o pipeline):
  - breadth.json      -> breadth proprietário (composite) + MM200/MM50  [já no repo]
  - data.json         -> CDI, NTN-B real, IPCA focus, cotas dos fundos, alfa [já no repo]
  - ibov_price.json   -> série do IBOV p/ drawdown do ATH               [já no repo]
  - Oceans14 (scrape) -> P/L trailing do IBOV                           [novo]
  - BCB SGS API       -> Selic meta (432) e IPCA 12m (13522)            [novo]
  - CVM inf_diario    -> PL/AUM dos fundos p/ monitor de decay          [novo]
  - config.json       -> caixa, carteira total, alvos, gates, e-mail    [você mantém]
"""

import json
import logging
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, date, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("alocador")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
TZ = "America/Sao_Paulo"

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIG  (defaults; sobrescritos por docs/../config.json e variáveis de ambiente)
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    # ── carteira ──────────────────────────────────────────────────────────────
    "caixa": 90000.0,                 # munição disponível hoje (R$)
    "carteira_total": 600000.0,       # patrimônio total incl. caixa (R$)  *ajuste*
    "aporte_mensal_pct": 0.035,       # ~3.5% do patrimônio/mês (informativo)

    # ── banda de caixa & ritmo (validados em backtest) ────────────────────────
    "piso_caixa_barato": 0.05,        # caixa-alvo quando mercado em pânico (score 10)
    "piso_caixa_caro":   0.20,        # caixa-alvo quando mercado caro (score 0)
    "lambda_deploy":     0.22,        # fração do gap caixa->piso deployada/semana
    "municao_minima":    0.05,        # nunca alocar abaixo disto (trava dura)

    # ── sleeves-alvo (carteira-alvo do regime atual) ──────────────────────────
    #   peso  = alvo % da carteira | atual = valor de mercado hoje (R$) p/ rotear
    #   gate  = "aberto" | "fechado" (Nu fechado até 2T26) | "saida" (drena)
    #   cap   = teto % da carteira
    "sleeves": {
        "Organon":  {"peso": 0.24, "atual": 150000, "gate": "aberto",  "cap": 0.26, "tipo": "fundo"},
        "Artica":   {"peso": 0.24, "atual": 150000, "gate": "aberto",  "cap": 0.26, "tipo": "fundo"},
        "ROXO34":   {"peso": 0.12, "atual": 72000,  "gate": "fechado", "cap": 0.13, "tipo": "acao",
                     "gate_motivo": "Aguardando 2T26 (checkpoint binário ago/26) — sem aporte"},
        "ALOS3":    {"peso": 0.08, "atual": 30000,  "gate": "aberto",  "cap": 0.10, "tipo": "acao"},
        "SPXR11":   {"peso": 0.14, "atual": 0,      "gate": "aberto",  "cap": 0.16, "tipo": "etf"},
        "HASH11":   {"peso": 0.02, "atual": 9000,   "gate": "aberto",  "cap": 0.03, "tipo": "etf"},
    },
    # núcleo de boutiques (p/ alerta de concentração pessoa-chave)
    "nucleo_sleeves": ["Organon", "Artica"],
    "nucleo_cap": 0.50,

    # ── monitor de decay ──────────────────────────────────────────────────────
    "organon_aum_teto": 1_000_000_000.0,   # R$1bi declarado
    "artica_aum_ref":   437_000_000.0,

    # ── e-mail (use GitHub Secrets: SMTP_USER / SMTP_PASS) ────────────────────
    "email_to": "theo.fernandes10@gmail.com",

    # ── overrides manuais (opcionais) ─────────────────────────────────────────
    "pl_override": None,        # P/L IBOV manual se o scrape falhar
    "cape": 9.4,                # CAPE Brasil (multiplicador leve de regime)
    "fear_greed": None,         # 0-100; se None, não pontua sentimento
    "override_sinal": None,     # "HOLD" | "BUY" força o sinal
}

CNPJ = {"Organon": "49.984.812/0001-08", "Artica": "18.302.338/0001-63"}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    p = ROOT / "config.json"
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
            # merge raso + merge de sleeves
            for k, v in user.items():
                if k == "sleeves" and isinstance(v, dict):
                    for sk, sv in v.items():
                        cfg["sleeves"].setdefault(sk, {}).update(sv)
                else:
                    cfg[k] = v
            log.info("config.json carregado")
        except Exception as e:
            log.warning(f"config.json inválido ({e}); usando defaults")
    # env overrides p/ valores que mudam toda semana
    for env, key, cast in [("ALOC_CAIXA", "caixa", float),
                           ("ALOC_TOTAL", "carteira_total", float)]:
        if os.getenv(env):
            try: cfg[key] = cast(os.getenv(env))
            except Exception: pass
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
#  LEITURA DOS JSONs QUE O REPO JÁ GERA
# ──────────────────────────────────────────────────────────────────────────────

def read_json(name):
    try:
        return json.loads((DOCS / name).read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"{name} indisponível ({e})")
        return None


def from_breadth():
    """breadth proprietário (composite 0-1) + MM200/MM50 do breadth.json."""
    b = read_json("breadth.json") or {}
    lat = b.get("latest", {})
    return {
        "composite": lat.get("composite"),      # 0-1 (baixo = poucas ações fortes = barato)
        "mm200": lat.get("breadth_200"),         # fração 0-1
        "mm50": lat.get("breadth_50"),
        "regime": lat.get("regime"),
        "date": lat.get("date"),
        "n": lat.get("n_constituents"),
    }


def from_data():
    """CDI, NTN-B real, IPCA focus, cotas/alfa dos fundos do data.json."""
    d = read_json("data.json") or {}
    funds = {}
    for f in d.get("funds", []):
        nm = (f.get("name") or "").lower()
        key = "Organon" if "organon" in nm else "Artica" if "artica" in nm or "ártica" in nm else None
        if key:
            funds[key] = f
    return {
        "cdi_cagr12": (d.get("cdi") or {}).get("cagr12"),
        "ntnb_long": (d.get("ntnb") or {}).get("ntnb_rate_long"),
        "ipca_focus": d.get("ipca_focus"),
        "funds": funds,
        "anchor": d.get("anchorDate"),
    }


def ibov_drawdown():
    """drawdown do IBOV vs máximo histórico, a partir do ibov_price.json."""
    j = read_json("ibov_price.json") or {}
    data = j.get("data", [])
    if not data:
        return None, None, None
    closes = [r["close"] for r in data if r.get("close")]
    if not closes:
        return None, None, None
    cur = closes[-1]; ath = max(closes)
    return (cur / ath - 1.0) * 100.0, cur, ath


# ──────────────────────────────────────────────────────────────────────────────
#  FETCHERS NOVOS  (P/L, Selic, IPCA, PL dos fundos) — todos com fallback
#  Obs.: a sandbox de teste não alcança esses domínios; a LÓGICA é testável,
#        os fetches ao vivo rodam no GitHub Actions. Cada um degrada com graça.
# ──────────────────────────────────────────────────────────────────────────────

def _requests():
    import requests  # import tardio p/ não quebrar se ausente em teste
    return requests

def fetch_pl_ibov(cfg):
    """P/L trailing do IBOV via Oceans14 (scrape leve). Fallback: override/None."""
    if cfg.get("pl_override"):
        return float(cfg["pl_override"]), "override manual"
    try:
        r = _requests().get(
            "https://oceans14.com.br/acoes/historico-pl-bovespa",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        # procura o primeiro número plausível de P/L (5..40) próximo de "P/L"
        cands = re.findall(r"(\d{1,2}[.,]\d{1,2})", r.text)
        vals = [float(c.replace(",", ".")) for c in cands]
        vals = [v for v in vals if 4 <= v <= 40]
        if vals:
            return vals[0], "oceans14"
    except Exception as e:
        log.warning(f"P/L Oceans14 falhou ({e})")
    return None, "indisponível"

def fetch_bcb_sgs(series, default=None):
    """Último valor de uma série do SGS do Banco Central (Selic 432, IPCA 13522)."""
    try:
        url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series}/dados/ultimos/1"
               f"?formato=json")
        r = _requests().get(url, timeout=20)
        return float(r.json()[-1]["valor"].replace(",", "."))
    except Exception as e:
        log.warning(f"BCB SGS {series} falhou ({e}); usando default {default}")
        return default

def fetch_fund_aum(cnpj_fmt):
    """PL (AUM) mais recente do fundo na CVM (inf_diario). Fallback: None."""
    import io, zipfile
    req = _requests()
    now = datetime.now()
    for off in range(0, 3):
        y = now.year; m = now.month - off
        while m <= 0: m += 12; y -= 1
        yyyymm = f"{y}{m:02d}"
        try:
            url = ("https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/"
                   f"inf_diario_fi_{yyyymm}.zip")
            r = req.get(url, timeout=60)
            if r.status_code != 200:
                continue
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            name = [n for n in zf.namelist() if n.lower().endswith(".csv")][0]
            import csv as _csv
            text = zf.read(name).decode("latin-1").splitlines()
            rdr = _csv.reader(text, delimiter=";")
            hdr = next(rdr)
            ci = next((i for i, h in enumerate(hdr) if h.upper().startswith("CNPJ_FUNDO")), 1)
            pi = hdr.index("VL_PATRIM_LIQ") if "VL_PATRIM_LIQ" in hdr else 6
            di = hdr.index("DT_COMPTC") if "DT_COMPTC" in hdr else 3
            last = None
            for row in rdr:
                if len(row) > pi and row[ci] == cnpj_fmt:
                    if last is None or row[di] > last[di]:
                        last = row
            if last:
                return float(last[pi].replace(",", ".")), last[di]
        except Exception as e:
            log.warning(f"AUM CVM {yyyymm} falhou ({e})")
    return None, None


# ──────────────────────────────────────────────────────────────────────────────
#  SCORING  —  4 grupos de fatores ~ortogonais, cada subscore 0..10
#  (0 = caro/ruim p/ comprar | 10 = barato/ótimo p/ comprar)
# ──────────────────────────────────────────────────────────────────────────────

def _tier(x, tiers):
    """tiers: lista [(limite, score, rótulo)] avaliada em ordem; usa o 1º limite>x."""
    for lim, s, lab in tiers:
        if x < lim:
            return s, lab
    return tiers[-1][1], tiers[-1][2]

def score_erp(ey, real_cdi):
    """ERP real = earnings yield (1/PL) - CDI real. Métrica colada no seu hurdle."""
    if ey is None or real_cdi is None:
        return None, "P/L ou CDI real ausente", None
    erp = ey - real_cdi
    s, lab = _tier(erp, [(-6, 0.5, "juro real muito superior"),
                         (-4, 2.0, "juro real bem superior"),
                         (-2, 3.5, "juro real claramente superior"),
                         (0, 5.5, "juro real levemente superior"),
                         (2, 7.0, "bolsa levemente acima do juro real"),
                         (4, 8.0, "bolsa acima do juro real"),
                         (6, 9.0, "bolsa claramente acima"),
                         (1e9, 10.0, "bolsa supera muito o juro real")])
    return s, f"ERP real {erp:+.1f}pp — {lab}", erp

def score_pl(pl):
    if pl is None: return None, "P/L ausente", None
    s, lab = _tier(pl, [(7, 10.0, "extremamente barato"), (9, 8.5, "muito barato"),
                        (11, 7.0, "barato"), (12, 6.0, "levemente barato"),
                        (13, 5.5, "abaixo do justo"), (14, 5.0, "justo"),
                        (15, 4.0, "levemente caro"), (17, 2.5, "caro"),
                        (19, 1.5, "muito caro"), (1e9, 0.5, "caro/trough de lucro")])
    return s, f"{pl:.1f}x — {lab}", pl

def score_dy(dy):
    if dy is None: return None, "DY ausente", None
    s, lab = _tier(-dy, [(-9, 10.0, "altíssimo"), (-7, 8.5, "muito atrativo"),
                         (-5.5, 7.0, "atrativo"), (-4, 5.0, "razoável"),
                         (-3, 3.0, "justo"), (-2, 1.5, "baixo"), (1e9, 0.0, "muito baixo")])
    return s, f"{dy:.1f}% — {lab}", dy

def score_ibov_dd(dd):
    if dd is None: return None, "DD IBOV ausente", None
    a = abs(dd)
    s, lab = _tier(a, [(5, 1.0, "próximo do ATH"), (10, 2.5, "perto do topo"),
                       (17, 4.5, "correção leve"), (25, 6.0, "correção relevante"),
                       (35, 7.5, "correção forte"), (45, 9.0, "queda severa"),
                       (1e9, 10.0, "crise histórica")])
    return s, f"{dd:.1f}% — {lab}", dd

def score_fund_dd(funds):
    """drawdown médio dos fundos núcleo (cota atual vs maxQuota do data.json)."""
    dds = []
    for k, f in funds.items():
        lq, mq = f.get("latestQuota"), f.get("maxQuota")
        if lq and mq and mq > 0:
            dds.append((lq / mq - 1.0) * 100.0)
    if not dds: return None, "DD fundos ausente", None
    avg = sum(dds) / len(dds); a = abs(avg)
    s, lab = _tier(a, [(3, 1.5, "em/acima do ATH"), (7, 2.5, "próximos do ATH"),
                       (14, 4.5, "correção leve"), (22, 6.0, "correção relevante"),
                       (30, 7.5, "correção forte"), (40, 9.0, "queda muito forte"),
                       (1e9, 10.0, "queda severa")])
    return s, f"DD médio {avg:.1f}% — {lab}", avg

def score_breadth_composite(comp):
    """breadth proprietário: composite 0-1 (baixo=poucas ações fortes=barato)."""
    if comp is None: return None, "breadth ausente", None
    pct = comp * 100.0
    s, lab = _tier(pct, [(20, 10.0, "extrema oportunidade"), (35, 8.5, "boa oportunidade"),
                         (50, 6.0, "abaixo do meio"), (65, 4.0, "neutro"),
                         (80, 2.0, "momentum positivo"), (1e9, 0.5, "esticado")])
    return s, f"{pct:.0f}% composto — {lab}", pct

def score_mm200(frac):
    if frac is None: return None, "MM200 ausente", None
    pct = frac * 100.0
    s, lab = _tier(pct, [(10, 10.0, "muito oversold"), (20, 8.5, "oversold"),
                         (35, 7.0, "fraqueza técnica"), (50, 5.0, "abaixo da média"),
                         (65, 3.0, "neutro"), (80, 1.5, "positivo"), (1e9, 0.5, "esticado")])
    return s, f"{pct:.0f}% > MM200 — {lab}", pct

def score_fear_greed(fg):
    if fg is None: return None, "F&G não informado", None
    s, lab = _tier(fg, [(10, 10.0, "medo extremo"), (20, 9.0, "medo muito alto"),
                        (30, 8.0, "medo alto"), (40, 7.0, "medo moderado"),
                        (50, 6.0, "leve medo"), (60, 5.0, "neutro"), (70, 4.0, "leve ganância"),
                        (80, 3.0, "ganância"), (90, 2.0, "ganância alta"), (1e9, 0.5, "ganância extrema")])
    return s, f"{fg:.0f}/100 — {lab}", fg

def cape_multiplier(cape):
    if cape is None: return 1.0, "CAPE não informado"
    m, lab = _tier(cape, [(10, 1.15, "+15%"), (14, 1.08, "+8%"), (18, 1.0, "neutro"),
                          (22, 0.95, "-5%"), (1e9, 0.88, "-12%")])
    return m, f"CAPE {cape:.1f}x — {lab}"


# ── pesos dos 4 grupos (somam 1.00) ───────────────────────────────────────────
#   Valuation 40% (ERP real é o coração, colado no hurdle CDI)
#   Drawdown/contrarian 32% (sinal limpo e robusto — validado no backtest)
#   Breadth 18% (seu indicador proprietário + MM200)
#   Sentimento 10% (F&G, opcional)
WEIGHTS = {
    "erp":          0.22, "pl":           0.10, "dy":           0.08,   # Valuation 0.40
    "ibov_dd":      0.16, "fund_dd":      0.16,                         # Drawdown  0.32
    "breadth":      0.12, "mm200":        0.06,                         # Breadth   0.18
    "fear_greed":   0.10,                                               # Sentimento 0.10
}
GROUPS = {
    "Valuation": ["erp", "pl", "dy"],
    "Drawdown/contrarian": ["ibov_dd", "fund_dd"],
    "Breadth": ["breadth", "mm200"],
    "Sentimento": ["fear_greed"],
}


def composite_score(scores, cape):
    """média ponderada dos subscores disponíveis (renormaliza se faltar algum)."""
    num = den = 0.0
    missing = []
    for k, w in WEIGHTS.items():
        s = scores.get(k, (None,))[0]
        if s is None:
            missing.append(k); continue
        num += s * w; den += w
    if den == 0:
        return None, missing, "BAIXA", 1.0, ""
    raw = num / den
    mult, capenote = cape_multiplier(cape)
    comp = max(0.0, min(10.0, raw * mult))
    conf = "ALTA" if den >= 0.75 else "MÉDIA" if den >= 0.5 else "BAIXA"
    return round(comp, 2), missing, conf, mult, capenote


# ──────────────────────────────────────────────────────────────────────────────
#  MOTOR DE DEPLOY  —  gap geométrico até um piso-alvo dependente da barateza
# ──────────────────────────────────────────────────────────────────────────────

def deploy_decision(comp, cfg):
    """
    Retorna quanto deployar nesta rodada (R$) e o caixa-alvo.
      piso-alvo  = interpola entre piso_caro (score 0) e piso_barato (score 10)
      gap        = caixa% - piso-alvo
      tranche    = lambda * gap * carteira  (>=0; trava em munição_minima)
    Aproximação geométrica do piso -> nunca zera, acelera quando barato.
    """
    total = max(1.0, float(cfg["carteira_total"]))
    cash = float(cfg["caixa"])
    cashpct = cash / total

    if cfg.get("override_sinal") == "HOLD":
        return dict(sinal="OVERRIDE: HOLD", cor="#6b7280", tranche=0.0,
                    caixa_alvo_pct=cashpct, gap=0.0, cashpct=cashpct, desc="override manual")
    if comp is None:
        return dict(sinal="INCOMPLETO", cor="#6b7280", tranche=0.0,
                    caixa_alvo_pct=cashpct, gap=0.0, cashpct=cashpct,
                    desc="faltam indicadores p/ score")

    ch = comp / 10.0  # barateza 0..1
    piso = cfg["piso_caixa_caro"] - (cfg["piso_caixa_caro"] - cfg["piso_caixa_barato"]) * ch
    piso = max(cfg["municao_minima"], piso)
    gap = cashpct - piso
    tranche = max(0.0, cfg["lambda_deploy"] * gap * total)
    # nunca furar a munição mínima
    tranche = min(tranche, max(0.0, cash - cfg["municao_minima"] * total))

    if comp < 3.5:    sinal, cor = "HOLD", "#b4322a"
    elif comp < 5.5:  sinal, cor = "CAUTELOSO", "#c2541f"
    elif comp < 6.5:  sinal, cor = "NEUTRO", "#b07d0a"
    elif comp < 7.5:  sinal, cor = "BOM", "#1a7a52"
    elif comp < 8.5:  sinal, cor = "ATRATIVO", "#15803d"
    elif comp < 9.5:  sinal, cor = "EXCELENTE", "#1d4ed8"
    else:             sinal, cor = "CRISE/RARO", "#6d28d9"

    # quantas rodadas p/ fechar ~63% do gap (1 - e^-1) ao ritmo lambda
    meia_vida = round(1.0 / cfg["lambda_deploy"]) if cfg["lambda_deploy"] > 0 else None

    return dict(sinal=sinal, cor=cor, tranche=round(tranche, -2),
                caixa_alvo_pct=piso, gap=gap, cashpct=cashpct,
                ritmo_rodadas=meia_vida,
                desc=f"piso-alvo de caixa {piso*100:.0f}% · deploya {cfg['lambda_deploy']*100:.0f}% do gap")


def sleeve_split(tranche, cfg):
    """
    Distribui a tranche entre sleeves: prioriza quem está mais ABAIXO do alvo
    (rebalanceamento), respeitando gate fechado (Nu) e caps por sleeve.
    """
    total = max(1.0, float(cfg["carteira_total"]))
    rows = []
    for name, s in cfg["sleeves"].items():
        atual_pct = s.get("atual", 0) / total
        alvo_pct = s["peso"]
        sub = alvo_pct - atual_pct           # quanto falta p/ atingir o alvo (pode ser <0)
        rows.append(dict(name=name, gate=s.get("gate", "aberto"),
                         alvo=alvo_pct, atual=atual_pct, sub=sub,
                         cap=s.get("cap", 1.0), tipo=s.get("tipo", ""),
                         motivo=s.get("gate_motivo", "")))
    # elegíveis = gate aberto e ainda abaixo do alvo e abaixo do cap
    elig = [r for r in rows if r["gate"] == "aberto" and r["sub"] > 0 and r["atual"] < r["cap"]]
    needed = sum(r["sub"] for r in elig)
    aloc = {r["name"]: 0.0 for r in rows}
    if tranche > 0 and needed > 0:
        for r in elig:
            share = r["sub"] / needed
            val = min(tranche * share, max(0.0, (r["cap"] - r["atual"]) * total))
            aloc[r["name"]] = round(val, -2)
    return rows, aloc


# ──────────────────────────────────────────────────────────────────────────────
#  MONITOR DE DECAY  (capacity / pessoa-chave)
# ──────────────────────────────────────────────────────────────────────────────

def decay_monitor(cfg, data):
    flags = []
    funds = data["funds"]
    # 1) Organon perto do teto de AUM
    aum_org, dt_org = fetch_fund_aum(CNPJ["Organon"])
    if aum_org:
        pct = aum_org / cfg["organon_aum_teto"]
        if pct >= 0.85:
            flags.append(("alto", f"Organon AUM R${aum_org/1e6:.0f}M = {pct*100:.0f}% do teto "
                                  f"declarado de R$1bi — risco de capacity (gestor deveria fechar)."))
        elif pct >= 0.70:
            flags.append(("medio", f"Organon AUM R${aum_org/1e6:.0f}M ({pct*100:.0f}% do teto) — "
                                   f"monitorar ritmo de captação."))
    # 2) alfa rolante vs CDI (do data.json) — proxy de "edge intacto"
    for k in cfg["nucleo_sleeves"]:
        f = funds.get(k, {})
        a = f.get("alphaVsCdi")
        if a is not None and a < 0:
            flags.append(("medio", f"{k}: alfa vs CDI 60m em {a:+.1f}pp — janela de revisão "
                                   f"(checar se é ciclo ou erosão estrutural)."))
    # 3) concentração do núcleo
    nucleo_pct = sum(cfg["sleeves"][s].get("atual", 0) for s in cfg["nucleo_sleeves"]) / max(1.0, cfg["carteira_total"])
    if nucleo_pct > cfg["nucleo_cap"]:
        flags.append(("medio", f"Núcleo {','.join(cfg['nucleo_sleeves'])} em {nucleo_pct*100:.0f}% "
                               f"(>{cfg['nucleo_cap']*100:.0f}% cap) — direcione aportes novos "
                               f"aos sleeves sub-alocados, não às boutiques."))
    if not flags:
        flags.append(("ok", "Sem alertas de capacity/decay nesta rodada."))
    return flags, aum_org


# ──────────────────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────────────────

def build():
    cfg = load_config()
    breadth = from_breadth()
    data = from_data()
    dd_ibov, ibov_cur, ibov_ath = ibov_drawdown()

    # juros / inflação (BCB primário; data.json como fallback)
    selic = fetch_bcb_sgs(432, default=cfg.get("selic_override"))
    _ipca_fb = data.get("ipca_focus")
    if isinstance(_ipca_fb, dict):
        _ipca_fb = _ipca_fb.get("ipca_12m")
    ipca = fetch_bcb_sgs(13522, default=cfg.get("ipca_override") or _ipca_fb)
    real_cdi = (selic * 0.98 - ipca) if (selic is not None and ipca is not None) else None

    pl, pl_src = fetch_pl_ibov(cfg)
    ey = (100.0 / pl) if pl else None
    dy = cfg.get("dy_override")

    scores = {
        "erp":        score_erp(ey, real_cdi),
        "pl":         score_pl(pl),
        "dy":         score_dy(dy),
        "ibov_dd":    score_ibov_dd(dd_ibov),
        "fund_dd":    score_fund_dd(data["funds"]),
        "breadth":    score_breadth_composite(breadth["composite"]),
        "mm200":      score_mm200(breadth["mm200"]),
        "fear_greed": score_fear_greed(cfg.get("fear_greed")),
    }
    comp, missing, conf, capemult, capenote = composite_score(scores, cfg.get("cape"))
    decision = deploy_decision(comp, cfg)
    sleeves, aloc = sleeve_split(decision["tranche"], cfg)
    flags, aum_org = decay_monitor(cfg, data)

    # ── monta JSON de saída ───────────────────────────────────────────────────
    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "asof": breadth.get("date") or data.get("anchor"),
        "composite": comp, "confidence": conf, "missing": missing,
        "cape_note": capenote, "cape_mult": capemult,
        "macro": {"selic": selic, "ipca": ipca, "real_cdi": real_cdi,
                  "ntnb_long": data.get("ntnb_long"), "pl": pl, "pl_src": pl_src,
                  "earnings_yield": ey, "dy": dy,
                  "ibov": ibov_cur, "ibov_ath": ibov_ath, "ibov_dd": dd_ibov,
                  "regime": breadth.get("regime")},
        "decision": decision,
        "groups": {g: {"weight": sum(WEIGHTS[k] for k in ks),
                       "items": [{"key": k, "label": k, "score": scores[k][0],
                                  "read": scores[k][1], "weight": WEIGHTS[k]} for k in ks]}
                   for g, ks in GROUPS.items()},
        # bloco cru p/ a página recalcular client-side a partir dos inputs do usuário
        "inputs": {
            "pl": pl, "ey": ey, "dy": dy, "selic": selic, "ipca": ipca,
            "real_cdi": real_cdi, "erp": scores["erp"][2], "ibov_dd": dd_ibov,
            "fund_dd": scores["fund_dd"][2], "breadth_pct": scores["breadth"][2],
            "mm200_pct": scores["mm200"][2], "fear_greed": cfg.get("fear_greed"),
            "cape": cfg.get("cape"), "regime": breadth.get("regime"),
        },
        "weights": WEIGHTS, "groups_map": GROUPS,
        "caixa": cfg["caixa"], "carteira_total": cfg["carteira_total"],
        "banda": {"piso_barato": cfg["piso_caixa_barato"], "piso_caro": cfg["piso_caixa_caro"],
                  "lambda": cfg["lambda_deploy"], "municao_minima": cfg["municao_minima"]},
        "sleeves": [dict(s, aloc=aloc.get(s["name"], 0.0)) for s in sleeves],
        "decay_flags": [{"level": lv, "msg": m} for lv, m in flags],
        "aum_organon": aum_org,
    }
    (DOCS / "allocation.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    log.info(f"allocation.json: score={comp} {decision['sinal']} tranche=R${decision['tranche']:,.0f}")
    return out, cfg


def send_email(out, cfg):
    user = os.getenv("SMTP_USER"); pw = os.getenv("SMTP_PASS")
    if not (user and pw and cfg.get("email_to")):
        log.info("SMTP não configurado (SMTP_USER/SMTP_PASS) — pulando e-mail.")
        return
    from email_render import render_email           # módulo irmão
    html = render_email(out, cfg)
    msg = MIMEMultipart("alternative")
    d = out["decision"]
    msg["Subject"] = (f"[Alocador] {out.get('asof','')} · Score "
                      f"{out['composite']}/10 · {d['sinal']} · "
                      f"Tranche R${d['tranche']:,.0f}")
    msg["From"] = user; msg["To"] = cfg["email_to"]
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(user, pw); s.send_message(msg)
    log.info("e-mail enviado")


if __name__ == "__main__":
    out, cfg = build()
    if "--email" in sys.argv:
        try: send_email(out, cfg)
        except Exception as e: log.warning(f"e-mail falhou: {e}")
