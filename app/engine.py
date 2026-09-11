"""
IBOV Breadth Engine
- Fetches prices for full historical universe
- Computes MA20/50/200 per stock
- Aggregates breadth per date using correct period composition
- Handles: missing data, IPOs, delistings, NaN MAs, backfill of new tickers
"""

import time
import logging
import pandas as pd
import numpy as np
import yfinance as yf
from pathlib import Path
from datetime import datetime, timedelta

from data.ibov_composition import IBOV_COMPOSITION_HISTORY
from data.ticker_normalization import normalize_tickers, get_all_historical_tickers

logger = logging.getLogger(__name__)

DATA_DIR      = Path("data")
PRICES_PATH   = DATA_DIR / "prices.parquet"
BREADTH_PATH  = DATA_DIR / "breadth.parquet"
MA_WINDOWS    = [20, 50, 200]

# Minimum days of price history needed to compute MA200 with warmup
MA200_WARMUP_DAYS = 250


# ── Composition helpers ──────────────────────────────────────────────────────

def get_composition_df() -> pd.DataFrame:
    rows = []
    for p in IBOV_COMPOSITION_HISTORY:
        rows.append({
            "start":   pd.Timestamp(p["start"]),
            "end":     pd.Timestamp(p["end"]),
            "tickers": normalize_tickers(p["tickers"]),
        })
    return pd.DataFrame(rows)


def get_constituents_on_date(comp_df: pd.DataFrame, date: pd.Timestamp) -> list[str]:
    """Return the IBOV constituents valid on a given date."""
    mask = (comp_df["start"] <= date) & (comp_df["end"] >= date)
    rows = comp_df[mask]
    if rows.empty:
        return []
    # Use the most recent matching period (handles overlapping periods)
    return rows.sort_values("start").iloc[-1]["tickers"]


def get_current_composition_tickers() -> list[str]:
    """Return the tickers for the current/most recent IBOV period."""
    today = pd.Timestamp.today().normalize()
    comp_df = get_composition_df()
    mask = comp_df["start"] <= today
    active = comp_df[mask].sort_values("start")
    if active.empty:
        return []
    return active.iloc[-1]["tickers"]


# ── Price fetching ───────────────────────────────────────────────────────────

def fetch_prices(tickers: list[str], start: str, end: str, chunk_size: int = 8) -> pd.DataFrame:
    """
    Download adjusted close prices for all tickers.
    Chunked to avoid rate limits. Retries on failure.
    """
    all_frames = []
    chunks = [tickers[i:i+chunk_size] for i in range(0, len(tickers), chunk_size)]
    total  = len(chunks)

    for idx, chunk in enumerate(chunks):
        logger.info(f"Fetching chunk {idx+1}/{total}: {chunk}")
        for attempt in range(4):
            try:
                raw = yf.download(
                    chunk,
                    start=start,
                    end=end,
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
                if raw.empty:
                    break

                # yfinance 1.x always returns MultiIndex (field, ticker)
                if isinstance(raw.columns, pd.MultiIndex):
                    close = raw["Close"]
                    # Single ticker returns Series — convert to DataFrame
                    if isinstance(close, pd.Series):
                        close = close.to_frame(name=chunk[0])
                else:
                    close = raw[["Close"]].rename(columns={"Close": chunk[0]})

                all_frames.append(close)
                break

            except Exception as e:
                logger.warning(f"Attempt {attempt+1} failed for {chunk}: {e}")
                time.sleep(2 ** attempt)

        time.sleep(0.3)

    if not all_frames:
        return pd.DataFrame()

    prices = pd.concat(all_frames, axis=1)
    prices = prices.loc[:, ~prices.columns.duplicated()]
    prices = prices.sort_index()
    prices = prices.asfreq("B").ffill(limit=5)

    return prices


def load_or_fetch_prices(force_refresh: bool = False) -> pd.DataFrame:
    """Load cached prices or fetch from scratch."""
    if PRICES_PATH.exists() and not force_refresh:
        logger.info("Loading cached prices...")
        return pd.read_parquet(PRICES_PATH)

    logger.info("Fetching full price history (this takes a few minutes)...")

    all_tickers = get_all_historical_tickers(IBOV_COMPOSITION_HISTORY)
    logger.info(f"Universe size: {len(all_tickers)} tickers")

    # Fetch from 2013-07 to give MA200 room to warm up before Jan 2014
    start = "2013-07-01"
    end   = datetime.today().strftime("%Y-%m-%d")

    prices = fetch_prices(all_tickers, start, end)

    DATA_DIR.mkdir(exist_ok=True)
    prices.to_parquet(PRICES_PATH)
    logger.info(f"Prices saved to {PRICES_PATH} — shape: {prices.shape}")

    return prices


# ── Backfill of new tickers ──────────────────────────────────────────────────

def backfill_missing_tickers(prices: pd.DataFrame) -> int:
    """
    After a rebalance, new tickers may be entirely absent from prices.parquet.
    This function fetches their full history from 2013-07-01 and merges it in.

    Only targets tickers that are completely absent from prices.parquet columns.
    Tickers already present (even with sparse data) are handled by
    historical_backfill(), which runs on every execution and is idempotent.

    Returns the number of tickers successfully backfilled.
    """
    current_tickers = get_current_composition_tickers()
    if not current_tickers:
        return 0

    # Only fetch tickers that don't exist in prices at all
    missing = [t for t in current_tickers if t not in prices.columns]

    if not missing:
        logger.info("Backfill: todos os tickers da composição atual já estão no parquet.")
        return 0

    logger.info(f"Backfill: {len(missing)} tickers novos ausentes do parquet: {missing}")

    full_start = "2013-07-01"
    end_date   = datetime.today().strftime("%Y-%m-%d")

    new_prices = fetch_prices(missing, start=full_start, end=end_date)
    if new_prices.empty:
        logger.warning("Backfill: nenhum dado retornado pelo Yahoo Finance.")
        return 0

    for col in new_prices.columns:
        prices[col] = new_prices[col]

    full_index = prices.index.union(new_prices.index)
    prices = prices.reindex(full_index).sort_index()
    prices = prices.asfreq("B").ffill(limit=5)

    prices.to_parquet(PRICES_PATH)
    logger.info(f"Backfill completo — prices.parquet atualizado: {prices.shape}")

    n_success = sum(
        1 for t in missing
        if t in new_prices.columns and new_prices[t].notna().sum() > 0
    )
    if n_success < len(missing):
        failed = [t for t in missing if t not in new_prices.columns or new_prices[t].notna().sum() == 0]
        logger.warning(f"Backfill falhou para {len(failed)} tickers: {failed}")

    return n_success


# ── Historical backfill of composition tickers missing from prices.parquet ───

def _has_truncated_history(prices: pd.DataFrame, ticker: str) -> bool:
    """
    True quando o ticker existe no parquet mas só com dados recentes demais
    para calcular MA200.

    Caso típico: um ticker entra na carteira e o incremental_update() cria a
    coluna com a janela curta do fetch diário (5 dias). Como a coluna passa a
    existir e tem dados, nem backfill_missing_tickers() (que só olha colunas
    ausentes) nem o teste antigo de `notna().sum() == 0` voltavam a tocar-lhe
    — e o ativo ficava fora do breadth para sempre.
    """
    if ticker not in prices.columns:
        return True
    serie = prices[ticker].dropna()
    if serie.empty:
        return True
    return len(serie) < MA200_WARMUP_DAYS


def historical_backfill() -> int:
    """
    One-shot backfill for composition tickers that are absent from
    prices.parquet, have zero valid rows, or carry a history too short for
    MA200. Fetches full history from 2013-07-01 so MA200 warm-up is available
    for the earliest IBOV windows.

    Called by fetch_breadth.py on every run; exits quickly when nothing is
    missing (all-tickers check is cheap). Triggers a full breadth recompute
    when it adds new data.

    Returns the number of tickers successfully backfilled.
    """
    prices = load_or_fetch_prices()

    all_comp_tickers = get_all_historical_tickers(IBOV_COMPOSITION_HISTORY)

    # Tickers ausentes, sem linhas válidas, ou com histórico curto demais
    need_fetch = [t for t in all_comp_tickers if _has_truncated_history(prices, t)]

    if not need_fetch:
        logger.info("Historical backfill: nada a fazer — todos os tickers presentes.")
        return 0

    logger.info(
        f"Historical backfill: {len(need_fetch)} tickers ausentes — "
        f"buscando histórico desde 2013-07-01..."
    )
    logger.info(f"  Tickers: {sorted(need_fetch)}")

    start = "2013-07-01"
    end   = datetime.today().strftime("%Y-%m-%d")

    new_prices = fetch_prices(need_fetch, start=start, end=end)
    if new_prices.empty:
        logger.warning("Historical backfill: yfinance não retornou dados.")
        return 0

    n_success = 0
    for col in new_prices.columns:
        n_valid = new_prices[col].notna().sum()
        if n_valid == 0:
            logger.warning(f"  {col}: sem dados no Yahoo Finance — mantendo ausente.")
            continue

        antes  = int(prices[col].notna().sum()) if col in prices.columns else 0
        merged = (
            new_prices[col].combine_first(prices[col])
            if col in prices.columns
            else new_prices[col]
        )
        depois = int(merged.notna().sum())

        if depois <= antes:
            # O Yahoo não tem histórico mais antigo — caso normal de ticker
            # renomeado (EMBJ3, MOTV3). Não conta como sucesso, senão o
            # breadth seria recomputado do zero em todas as execuções.
            logger.info(f"  {col}: sem histórico adicional no Yahoo ({antes} linhas).")
            continue

        prices[col] = merged
        n_success  += 1
        logger.info(f"  {col}: {antes} → {depois} linhas.")

    if n_success > 0:
        full_index = prices.index.union(new_prices.index)
        prices = prices.reindex(full_index).sort_index()
        prices = prices.asfreq("B").ffill(limit=5)
        prices.to_parquet(PRICES_PATH)
        logger.info(
            f"Historical backfill: {n_success}/{len(need_fetch)} tickers adicionados. "
            f"Recomputando breadth completo..."
        )
        breadth = compute_breadth(prices)
        breadth.to_parquet(BREADTH_PATH)
        logger.info(f"Breadth recomputado: {breadth.shape}")

    return n_success


# ── MA computation ───────────────────────────────────────────────────────────

def compute_moving_averages(prices: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """
    Compute rolling MAs for each window.
    min_periods=window ensures no MA until enough history exists.
    This prevents IPO distortion.
    """
    mas = {}
    for w in MA_WINDOWS:
        mas[w] = prices.rolling(window=w, min_periods=w).mean()
    return mas


# ── Breadth computation ──────────────────────────────────────────────────────

def compute_breadth(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Compute daily breadth for MA20/50/200 using time-correct IBOV composition.

    For each trading day:
    1. Look up valid IBOV constituents for that date
    2. For each MA window, count stocks with price > MA
    3. Divide by valid (non-NaN) count

    Note on NaN breadth_200: tickers with <200 days of history will have NaN
    MA200. This is correct behaviour — we do not impute or interpolate.
    For recently renamed tickers (e.g. EMBJ3 = ex-EMBR3), Yahoo Finance only
    serves data from the rename date, so MA200 will be NaN until 200 trading
    days have elapsed. This is a Yahoo Finance data limitation, not a bug.
    """
    comp_df = get_composition_df()
    mas     = compute_moving_averages(prices)
    results = []

    for date in prices.index:
        constituents = get_constituents_on_date(comp_df, date)
        if not constituents:
            continue

        available = [t for t in constituents if t in prices.columns]
        if len(available) < 10:
            continue

        row = {"date": date}

        for w, ma in mas.items():
            px = prices.loc[date, available]
            m  = ma.loc[date, available]

            valid_mask = px.notna() & m.notna()
            n_valid    = valid_mask.sum()

            if n_valid < 5:
                row[f"breadth_{w}"] = None
                row[f"count_{w}"]   = 0
                continue

            above = (px[valid_mask] > m[valid_mask]).sum()
            row[f"breadth_{w}"] = float(above) / float(n_valid)
            row[f"count_{w}"]   = int(n_valid)

        row["n_constituents"] = len(available)
        results.append(row)

    df = pd.DataFrame(results).set_index("date")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    return df


def load_or_compute_breadth(force_refresh: bool = False) -> pd.DataFrame:
    """Load cached breadth or compute from scratch."""
    if BREADTH_PATH.exists() and not force_refresh:
        logger.info("Loading cached breadth data...")
        return pd.read_parquet(BREADTH_PATH)

    logger.info("Computing breadth (this is the slow step)...")
    prices  = load_or_fetch_prices()
    breadth = compute_breadth(prices)

    breadth.to_parquet(BREADTH_PATH)
    logger.info(f"Breadth saved — shape: {breadth.shape}")

    return breadth


# ── Incremental daily update ─────────────────────────────────────────────────

def incremental_update() -> pd.DataFrame:
    """
    Atualiza preços e recomputa breadth completo a cada execução.

    Recomputa o breadth inteiro a partir do prices.parquet — sem cache,
    sem janelas parciais, sem risco de gaps ou stale rows. O recompute
    leva ~30s e é a única abordagem que garante consistência permanente
    independente do estado do repositório no checkout.
    """
    logger.info("Running incremental update (full breadth recompute)...")

    prices          = load_or_fetch_prices()
    last_price_date = prices.index.max()
    today           = pd.Timestamp.today().normalize()
    all_tickers     = get_all_historical_tickers(IBOV_COMPOSITION_HISTORY)

    # Busca preços recentes (últimos 5 dias para correções tardias)
    fetch_start = (last_price_date - timedelta(days=5)).strftime("%Y-%m-%d")
    fetch_end   = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    new_px = fetch_prices(all_tickers, start=fetch_start, end=fetch_end)

    if not new_px.empty:
        for col in new_px.columns:
            if col not in prices.columns:
                prices[col] = new_px[col]
            else:
                prices.update({col: new_px[col]})

        new_rows = new_px[~new_px.index.isin(prices.index)]
        if not new_rows.empty:
            prices = pd.concat([prices, new_rows])

        prices = prices.sort_index()
        prices.to_parquet(PRICES_PATH)
        logger.info(f"Prices updated to {prices.index.max().date()}")

    # Recomputa breadth completo — garante zero gaps e zero stale rows
    logger.info(f"Recomputando breadth completo ({len(prices)} dias de preços)...")
    breadth = compute_breadth(prices)

    stale = ((breadth['count_20'] == 0) & (breadth['n_constituents'] > 5)).sum()
    if stale > 0:
        logger.warning(f"{stale} stale rows detectadas — prices.parquet pode estar incompleto para alguns tickers")
    else:
        logger.info(f"Breadth OK: {len(breadth)} linhas, zero gaps, até {breadth.index.max().date()}")

    breadth.to_parquet(BREADTH_PATH)
    return breadth


# ── Regime classification ────────────────────────────────────────────────────

def classify_regime(breadth_200: float | None) -> str:
    if breadth_200 is None or np.isnan(breadth_200):
        return "unknown"
    if breadth_200 >= 0.80:
        return "overbought"
    if breadth_200 >= 0.60:
        return "bull"
    if breadth_200 >= 0.40:
        return "neutral"
    if breadth_200 >= 0.20:
        return "bear"
    return "capitulation"
