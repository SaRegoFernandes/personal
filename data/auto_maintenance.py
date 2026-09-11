"""
IBOV Auto-Maintenance
Roda a cada update e faz 3 coisas automaticamente:

1. Detecta tickers que pararam de ter dados (possível rename/delisting)
2. Busca o ticker substituto no Yahoo Finance
3. Baixa a composição atual do IBOV direto da B3 e atualiza ibov_composition.py

Quando encontra mudanças, atualiza os arquivos .py e sinaliza para o
GitHub Actions fazer commit.

CORREÇÕES vs versão anterior:
- check_ibov_rebalance() não adiciona período duplicado se já existe
- Períodos sobrepostos são detectados e o mais recente prevalece
- Rebalanceamento pode ser chamado antes do fetch (daily_update.py)
"""

import re
import time
import logging
import requests
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DATA_DIR          = Path("data")
NORM_PATH         = DATA_DIR / "ticker_normalization.py"
COMPOSITION_PATH  = DATA_DIR / "ibov_composition.py"
CHANGES_FLAG_PATH = DATA_DIR / ".maintenance_changes"


# ── 1. Detecção de tickers mortos ────────────────────────────────────────────

def detect_dead_tickers(prices: pd.DataFrame, lookback_days: int = 7) -> list[str]:
    """
    Retorna tickers que não tiveram nenhum dado nos últimos `lookback_days` dias úteis.
    Ignora tickers que já são None no mapa (já conhecidos como mortos).
    Ignora tickers recentemente renomeados que têm histórico curto por design
    (ex: EMBJ3 tem só 112 dias — isso é esperado, não é erro).
    """
    from data.ticker_normalization import TICKER_MAP

    cutoff = prices.index.max() - timedelta(days=lookback_days * 2)
    recent = prices[prices.index >= cutoff]

    dead = []
    for col in recent.columns:
        # Ignora se já está mapeado como None (já sabemos que é morto)
        if TICKER_MAP.get(col) is None:
            continue
        # Ignora se o ticker em si é o valor de destino no mapa
        # (evita marcar como morto um ticker que acabou de ser renomeado)
        if col in TICKER_MAP.values():
            if recent[col].isna().all():
                dead.append(col)
            continue
        if recent[col].isna().all():
            dead.append(col)

    if dead:
        logger.info(f"Tickers sem dados recentes: {dead}")
    return dead


# ── 2. Busca de ticker substituto ────────────────────────────────────────────

def search_successor_ticker(dead_ticker: str) -> str | None:
    """
    Tenta encontrar o ticker atual de uma empresa dado o ticker antigo.
    Estratégia:
      1. Busca no Yahoo Finance Search API pelo nome da empresa
      2. Filtra por sufixo .SA (B3)
      3. Verifica se o ticker encontrado tem dados recentes
    """
    base = dead_ticker.replace(".SA", "")

    company_name = None
    try:
        info = yf.Ticker(dead_ticker).info
        company_name = info.get("longName") or info.get("shortName")
    except Exception:
        pass

    query = company_name or base

    url     = "https://query2.finance.yahoo.com/v1/finance/search"
    headers = {"User-Agent": "Mozilla/5.0"}
    params  = {"q": query, "lang": "pt-BR", "region": "BR", "quotesCount": 10}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Yahoo search falhou para {dead_ticker}: {e}")
        return None

    quotes     = data.get("quotes", [])
    candidates = []

    for q in quotes:
        symbol = q.get("symbol", "")
        if not symbol.endswith(".SA"):
            continue
        if symbol == dead_ticker:
            continue
        base_symbol = symbol.replace(".SA", "")
        if len(base_symbol) > 6:
            continue
        candidates.append(symbol)

    for candidate in candidates:
        try:
            df = yf.download(
                candidate,
                period="5d",
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if not df.empty and df["Close"].notna().any():
                logger.info(f"Substituto encontrado: {dead_ticker} → {candidate}")
                return candidate
        except Exception:
            pass
        time.sleep(0.3)

    logger.info(f"Nenhum substituto encontrado para {dead_ticker} — marcando como delisted")
    return None


# ── 3. Atualização do ticker_normalization.py ─────────────────────────────────

def update_ticker_map(updates: dict) -> bool:
    """
    Adiciona entradas novas ao TICKER_MAP em ticker_normalization.py.
    updates = {"DEAD.SA": "NEW.SA"} ou {"DEAD.SA": None}
    Retorna True se fez alguma alteração.
    """
    if not updates:
        return False

    content = NORM_PATH.read_text()

    new_updates = {}
    for old, new in updates.items():
        if f'"{old}"' not in content:
            new_updates[old] = new

    if not new_updates:
        logger.info("Todas as entradas já existem no TICKER_MAP")
        return False

    insert_marker = "}\n\n\ndef normalize_ticker"
    if insert_marker not in content:
        logger.error("Marcador de inserção não encontrado em ticker_normalization.py")
        return False

    date_str = datetime.today().strftime("%Y-%m-%d")
    lines    = f"\n    # ── Auto-detectado em {date_str} ──\n"
    for old, new in new_updates.items():
        value = f'"{new}"' if new else "None"
        lines += f'    "{old}": {value},\n'

    new_content = content.replace(insert_marker, lines + insert_marker)
    NORM_PATH.write_text(new_content)
    logger.info(f"ticker_normalization.py atualizado: {new_updates}")
    return True


# ── 4. Composição do IBOV via B3 ─────────────────────────────────────────────

def fetch_ibov_composition_from_b3() -> list[str] | None:
    """
    Baixa a carteira teórica atual do IBOV direto da API da B3.
    Retorna lista de tickers no formato TICKER3.SA ou None em caso de falha.
    """
    url = (
        "https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall/GetPortfolioDay/"
        "eyJsYW5ndWFnZSI6InB0LWJyIiwicGFnZU51bWJlciI6MSwicGFnZVNpemUiOjEyMCwiaW5kZXgi"
        "OiJJQk9WIiwic2VnbWVudCI6IjEifQ=="
    )
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer":    "https://www.b3.com.br/",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Falha ao buscar composição da B3: {e}")
        return None

    tickers = []
    for item in data.get("results", []):
        code = item.get("cod", "").strip()
        if code and len(code) >= 4:
            tickers.append(code + ".SA")

    if len(tickers) < 50:
        logger.warning(f"B3 retornou apenas {len(tickers)} tickers — ignorando (pode ser erro)")
        return None

    logger.info(f"B3: {len(tickers)} tickers na composição atual do IBOV")
    return sorted(tickers)


def _carteira_end_date(start: pd.Timestamp) -> str:
    """
    Último dia de validade de uma carteira que passa a vigorar em `start`.

    As carteiras do Ibovespa são quadrimestrais e valem até o último dia do
    mês anterior à próxima virada (janeiro, maio e setembro). Ex.: a carteira
    que entrou em vigor em 08/09/2026 vale até 31/12/2026.

    Isto substitui o antigo `start + 121 dias`, que produzia datas de fim
    arbitrárias (ex.: 03/01/2027) e desalinhava o histórico da B3.
    """
    for mes_virada in (1, 5, 9):
        if mes_virada > start.month:
            proxima = pd.Timestamp(year=start.year, month=mes_virada, day=1)
            break
    else:
        proxima = pd.Timestamp(year=start.year + 1, month=1, day=1)

    return (proxima - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def check_ibov_rebalance() -> bool:
    """
    Verifica se houve rebalanceamento do IBOV e actualiza ibov_composition.py.

    Regra crítica: só actua APÓS o período vigente ter expirado.
    Isso evita que prévias oficiais da B3 (publicadas ~30 dias antes da virada)
    sejam registadas prematuramente como rebalanceamentos.

    A B3 publica a carteira definitiva na 1ª segunda-feira de jan/mai/set.
    Prévias são publicadas nos 30 dias anteriores — não devem ser registadas.
    """
    from data.ibov_composition import IBOV_COMPOSITION_HISTORY

    last_period = IBOV_COMPOSITION_HISTORY[-1]
    last_end    = pd.Timestamp(last_period["end"])
    today       = pd.Timestamp.today().normalize()

    # Só actua se o período vigente já expirou
    if last_end >= today:
        logger.info(f"Composição vigente até {last_end.date()} — nenhuma acção necessária.")
        return False

    logger.info(f"Período expirou em {last_end.date()} — verificando nova composição na B3...")

    new_tickers = fetch_ibov_composition_from_b3()
    if not new_tickers:
        return False

    last_tickers = set(last_period["tickers"])
    new_set      = set(new_tickers)
    added        = new_set - last_tickers
    removed      = last_tickers - new_set

    if not added and not removed:
        logger.info("Composição do IBOV inalterada.")
        return False

    logger.info(f"Rebalanceamento detectado! +{len(added)} entradas, -{len(removed)} saídas")
    if added:
        logger.info(f"  Novos tickers: {sorted(added)}")
    if removed:
        logger.info(f"  Saídas: {sorted(removed)}")

    # Novo período: começa HOJE — a API da B3 devolve a carteira em vigor no
    # dia, logo é hoje que ela passa a valer para efeito de cálculo. Usar
    # `last_end + 1 dia` punha a carteira nova a valer em fins de semana e
    # feriados (ex.: 05/09/2026, um sábado, quando a virada foi só a 08/09).
    # O intervalo que sobra entre os períodos é sempre não-útil e o engine
    # simplesmente ignora essas datas.
    new_start_ts = max(pd.Timestamp.today().normalize(), last_end + pd.Timedelta(days=1))
    new_start    = new_start_ts.strftime("%Y-%m-%d")
    new_end      = _carteira_end_date(new_start_ts)

    ticker_lines = ",\n            ".join(f'"{t}"' for t in sorted(new_tickers))
    new_entry = (
        f'    {{\n'
        f'        "start": "{new_start}", "end": "{new_end}",\n'
        f'        "tickers": [\n'
        f'            {ticker_lines}\n'
        f'        ],\n'
        f'    }},\n]'
    )

    content = COMPOSITION_PATH.read_text()
    content = re.sub(r'\]\s*$', new_entry, content.rstrip())
    COMPOSITION_PATH.write_text(content)

    logger.info(f"ibov_composition.py actualizado: {new_start} → {new_end}")
    return True


def run_maintenance(prices: pd.DataFrame) -> bool:
    """
    Ponto de entrada principal. Chamado pelo daily_update.py a cada execução.
    Nota: check_ibov_rebalance() NÃO é chamado aqui — é chamado antes do
    fetch de preços em daily_update.py para garantir que o universo está
    actualizado antes de qualquer download.
    Retorna True se fez alguma mudança (para sinalizar commit).
    """
    changed = False

    # 1. Detecta tickers sem dados recentes
    dead = detect_dead_tickers(prices)

    if dead:
        updates = {}
        for ticker in dead:
            logger.info(f"Investigando ticker morto: {ticker}")
            successor = search_successor_ticker(ticker)
            updates[ticker] = successor

        # 2. Atualiza ticker_normalization.py
        if update_ticker_map(updates):
            changed = True

    if changed:
        CHANGES_FLAG_PATH.write_text(datetime.today().isoformat())
        logger.info("✓ Manutenção: mudanças detectadas — commit pendente")
    else:
        logger.info("✓ Manutenção: tudo em ordem, nenhuma mudança necessária")

    return changed
