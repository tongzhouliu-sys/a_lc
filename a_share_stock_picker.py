import datetime as dt
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_HALF_UP

import akshare as ak
import numpy as np
import pandas as pd


# ============================================================
# 配置区
# ============================================================
LOOKBACK_SURGE = 20
MIN_SURGE_PCT = 10.0
VOLUME_MULTIPLE = 1.5

LOOKBACK_GAP = 20
GAP_CONFIRM_DAYS = 3

MIN_BULLISH_DAYS = 3
MAX_DAILY_GAIN = 5.0

POSITION_DAYS = 250
MAX_POSITION = 0.50  # 严格小于50%；恰好50%不通过

MIN_HISTORY = 250
HISTORY_CALENDAR_DAYS = 500

MAIN_BOARD_ONLY = False  # True：仅沪深主板
EXCLUDE_ST = True
EXCLUDE_DELISTING = True

ACTION_DIFF_THRESHOLD = 2.0

WORKERS = 6
REQUEST_DELAY = 0.10
RETRY_TIMES = 3

# 时区：统一采用北京时间 (UTC+8)
BEIJING_TZ = dt.timezone(dt.timedelta(hours=8))

OUTPUT_DIR = "stock_picker_output"
RESULT_CSV = os.path.join(OUTPUT_DIR, "stock_candidates.csv")
HTML_REPORT = os.path.join(OUTPUT_DIR, "stock_candidates.html")
RUN_LOG = os.path.join(OUTPUT_DIR, "run.log")
ERROR_LOG = os.path.join(OUTPUT_DIR, "errors.log")

os.makedirs(OUTPUT_DIR, exist_ok=True)

logger = logging.getLogger("a_share_picker")
logger.setLevel(logging.INFO)
logger.handlers.clear()
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(threadName)s - %(message)s"
)
for path, level in [(RUN_LOG, logging.INFO), (ERROR_LOG, logging.ERROR)]:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)


RESULT_COLUMNS = [
    "代码", "名称", "最新交易日", "最新收盘价",
    "连续真阳线数量", "连续阳线起始日",
    "启动日", "启动日涨幅_pct", "启动日成交量",
    "启动日理论涨停价", "启动日后最低价",
    "跳空日", "缺口下沿", "缺口上沿", "缺口确认完成日",
    "250日最低价", "250日最高价", "250日区间位置_pct",
]


def get_beijing_now():
    """获取当前北京时间"""
    return dt.datetime.now(BEIJING_TZ)


def safe_float(value, default=np.nan):
    try:
        value = float(value)
        return value if np.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def normalize_code(code):
    return str(code).strip().zfill(6)


def get_market_prefix(code):
    """获取东方财富对应的前缀 (sh / sz / bj)"""
    code = normalize_code(code)
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh"
    elif code.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz"
    elif code.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


def is_main_board(code):
    code = normalize_code(code)
    return (
        code.startswith(("000", "001", "002", "003"))
        or code.startswith(("600", "601", "603", "605"))
    )


def get_limit_ratio(code, name=""):
    """简化的常见板块理论涨幅限制，不替代历史实际涨停价数据。"""
    code = normalize_code(code)
    name = str(name).upper()
    if "ST" in name or "退" in name:
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def round_price(price):
    return float(
        Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def theoretical_limit_up(previous_close, code, name=""):
    previous_close = safe_float(previous_close)
    if not np.isfinite(previous_close) or previous_close <= 0:
        return np.nan
    return round_price(previous_close * (1 + get_limit_ratio(code, name)))


def get_stock_list_fallback():
    """基础全量股票列表获取（含重试逻辑）"""
    logger.info("正在获取 A 股全量代码列表...")
    last_err = None
    for attempt in range(RETRY_TIMES):
        try:
            stocks = ak.stock_info_a_code_name()
            if stocks is not None and not stocks.empty:
                stocks = stocks.copy()
                stocks.columns = [str(c).strip() for c in stocks.columns]
                if "code" in stocks.columns and "name" in stocks.columns:
                    stocks["code"] = stocks["code"].map(normalize_code)
                    stocks["name"] = stocks["name"].astype(str).str.strip()
                    stocks = stocks[stocks["code"].str.fullmatch(r"\d{6}", na=False)].copy()
                    if EXCLUDE_ST:
                        stocks = stocks[~stocks["name"].str.contains(r"ST", case=False, na=False)]
                    if EXCLUDE_DELISTING:
                        stocks = stocks[~stocks["name"].str.contains(r"退", case=False, na=False)]
                    if MAIN_BOARD_ONLY:
                        stocks = stocks[stocks["code"].map(is_main_board)]
                    stocks = stocks.drop_duplicates(subset=["code"]).sort_values("code").reset_index(drop=True)
                    return stocks
        except Exception as exc:
            last_err = exc
            logger.warning("获取全量列表失败（尝试 %d/%d）：%s", attempt + 1, RETRY_TIMES, exc)
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"获取股票列表最终失败：{last_err}")


def get_stock_pool():
    """
    两阶段极速初筛机制：
    由于策略要求：当前最新交易日必须是真阳线（收盘 > 开盘）且涨幅在 (0, 5%]。
    在拉取 500 天历史 K 线前，先通过单次全市场快照接口直接过滤掉 80% 当天不满足阳线条件的股票，
    将耗时从 40~60 分钟缩短至 1~3 分钟，同时极大降低海外 IP 触发接口限流的概率。
    若快照接口获取失败，则优雅降级为全量代码池扫描。
    """
    logger.info("尝试拉取全市场实时/收盘快照进行第 1 阶段快速初筛...")
    for attempt in range(RETRY_TIMES):
        try:
            spot_df = ak.stock_zh_a_spot_em()
            if spot_df is not None and not spot_df.empty:
                spot_df = spot_df.copy()
                spot_df.columns = [str(c).strip() for c in spot_df.columns]

                code_col = next((c for c in ["代码", "code"] if c in spot_df.columns), None)
                name_col = next((c for c in ["名称", "name"] if c in spot_df.columns), None)
                close_col = next((c for c in ["最新价", "close"] if c in spot_df.columns), None)
                open_col = next((c for c in ["今开", "open"] if c in spot_df.columns), None)
                pct_col = next((c for c in ["涨跌幅", "pct_change"] if c in spot_df.columns), None)

                if all([code_col, name_col, close_col, open_col, pct_col]):
                    spot_df["code"] = spot_df[code_col].map(normalize_code)
                    spot_df["name"] = spot_df[name_col].astype(str).str.strip()
                    spot_df = spot_df[spot_df["code"].str.fullmatch(r"\d{6}", na=False)].copy()

                    if EXCLUDE_ST:
                        spot_df = spot_df[~spot_df["name"].str.contains(r"ST", case=False, na=False)]
                    if EXCLUDE_DELISTING:
                        spot_df = spot_df[~spot_df["name"].str.contains(r"退", case=False, na=False)]
                    if MAIN_BOARD_ONLY:
                        spot_df = spot_df[spot_df["code"].map(is_main_board)]

                    total_all = len(spot_df)
                    spot_df["_close"] = pd.to_numeric(spot_df[close_col], errors="coerce")
                    spot_df["_open"] = pd.to_numeric(spot_df[open_col], errors="coerce")
                    spot_df["_pct"] = pd.to_numeric(spot_df[pct_col], errors="coerce")

                    # 核心初筛规则：最新交易日必须是真阳线（收盘 > 开盘），且涨跌幅在 (0, MAX_DAILY_GAIN] 之间
                    cond_valid = (
                        (spot_df["_close"] > 0)
                        & (spot_df["_open"] > 0)
                        & (spot_df["_close"] > spot_df["_open"])
                        & (spot_df["_pct"] > 0)
                        & (spot_df["_pct"] <= MAX_DAILY_GAIN)
                    )
                    filtered_df = spot_df.loc[cond_valid, ["code", "name"]].drop_duplicates("code").reset_index(drop=True)
                    logger.info(
                        "两阶段初筛成功：全市场 %d 只股票中，仅有 %d 只符合【最新交易日收真阳线且涨幅 (0, %.1f%%]】，进入第 2 阶段历史精筛！",
                        total_all, len(filtered_df), MAX_DAILY_GAIN
                    )
                    return filtered_df
        except Exception as exc:
            logger.warning("全市场快照初筛失败（尝试 %d/%d）：%s", attempt + 1, RETRY_TIMES, exc)
            time.sleep(1.0 * (attempt + 1))

    logger.warning("快照初筛不可用，降级为全量代码扫描...")
    return get_stock_list_fallback()


def fetch_daily_history(code, start_date, end_date):
    last_error = None
    for attempt in range(RETRY_TIMES):
        try:
            time.sleep(REQUEST_DELAY)
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",  # 不复权
            )
            if df is None or df.empty:
                return pd.DataFrame()

            df = df.copy()
            df.columns = [str(c).strip() for c in df.columns]
            required = ["日期", "开盘", "收盘", "最高", "最低", "成交量"]
            missing = [c for c in required if c not in df.columns]
            if missing:
                raise ValueError(f"{code} 行情缺少字段 {missing}")

            rename_map = {
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "成交额": "amount", "涨跌幅": "pct_change",
                "涨跌额": "change", "换手率": "turnover",
            }
            df = df.rename(columns=rename_map)
            for col in [
                "open", "close", "high", "low", "volume", "amount",
                "pct_change", "change", "turnover",
            ]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date", "open", "close", "high", "low", "volume"])
            df = df.sort_values("date").drop_duplicates("date", keep="last")
            valid = (
                (df["open"] > 0) & (df["close"] > 0)
                & (df["high"] > 0) & (df["low"] > 0)
                & (df["volume"] >= 0)
                & (df["high"] >= df["low"])
                & (df["high"] >= df["open"])
                & (df["high"] >= df["close"])
                & (df["low"] <= df["open"])
                & (df["low"] <= df["close"])
            )
            return df.loc[valid].reset_index(drop=True)
        except Exception as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))

    logger.warning("%s 行情获取最终失败：%s", code, last_error)
    return pd.DataFrame()


def prepare_market_data(df):
    df = df.copy()
    df["raw_pct"] = df["close"].pct_change() * 100
    if "pct_change" in df.columns:
        df["pct_diff"] = (df["raw_pct"] - df["pct_change"]).abs()
        df["suspected_action"] = df["pct_diff"] > ACTION_DIFF_THRESHOLD
    else:
        df["pct_diff"] = np.nan
        df["suspected_action"] = False
    df["suspected_action"] = df["suspected_action"].fillna(False)
    return df


def check_low_position(df):
    if len(df) < POSITION_DAYS:
        return False, np.nan, np.nan, np.nan
    recent = df.tail(POSITION_DAYS)
    low_price = safe_float(recent["low"].min())
    high_price = safe_float(recent["high"].max())
    current_close = safe_float(df.iloc[-1]["close"])
    if (
        not np.isfinite(low_price) or not np.isfinite(high_price)
        or not np.isfinite(current_close) or high_price <= low_price
    ):
        return False, np.nan, low_price, high_price
    ratio = (current_close - low_price) / (high_price - low_price)
    return ratio < MAX_POSITION, ratio, low_price, high_price


def find_surge_days(df):
    """最近20个交易日内寻找启动日，不包含最新交易日。"""
    n = len(df)
    if n < 3:
        return []
    start_idx = max(1, n - 1 - LOOKBACK_SURGE)
    end_idx = n - 1
    found = []
    for i in range(start_idx, end_idx):
        row, prev = df.iloc[i], df.iloc[i - 1]
        pct = safe_float(row["raw_pct"])
        volume = safe_float(row["volume"])
        prev_volume = safe_float(prev["volume"])
        if not np.isfinite(pct) or pct < MIN_SURGE_PCT:
            continue
        if (
            not np.isfinite(volume) or not np.isfinite(prev_volume)
            or prev_volume <= 0 or volume < prev_volume * VOLUME_MULTIPLE
        ):
            continue
        if bool(row["suspected_action"]) or bool(prev["suspected_action"]):
            continue
        found.append(i)
    return found


def find_confirmed_gaps(df, surge_idx):
    """寻找启动日之后的向上跳空，后续3个交易日低点均不回补缺口下沿。"""
    n = len(df)
    first_gap_idx = surge_idx + 1
    last_gap_idx = min(n - GAP_CONFIRM_DAYS - 1, surge_idx + LOOKBACK_GAP)
    gaps = []
    if first_gap_idx > last_gap_idx:
        return gaps

    for gap_idx in range(first_gap_idx, last_gap_idx + 1):
        prev, gap_row = df.iloc[gap_idx - 1], df.iloc[gap_idx]
        prev_high = safe_float(prev["high"])
        gap_low = safe_float(gap_row["low"])
        if not np.isfinite(prev_high) or not np.isfinite(gap_low):
            continue
        if gap_low <= prev_high:
            continue
        if bool(gap_row["suspected_action"]) or bool(prev["suspected_action"]):
            continue

        confirm_idx = gap_idx + GAP_CONFIRM_DAYS
        if confirm_idx >= n:
            continue
        future = df.iloc[gap_idx + 1:confirm_idx + 1]
        if len(future) != GAP_CONFIRM_DAYS:
            continue
        if not (future["low"] > prev_high).all():
            continue

        gaps.append({
            "gap_idx": gap_idx,
            "confirm_idx": confirm_idx,
            "gap_lower": prev_high,
            "gap_upper": gap_low,
        })
    return gaps


def get_current_bullish_streak(df):
    """统计从最新交易日向前的连续真阳线；每根涨幅须在(0, 5%]。"""
    streak = 0
    start_idx = None
    for i in range(len(df) - 1, -1, -1):
        row = df.iloc[i]
        op = safe_float(row["open"])
        close = safe_float(row["close"])
        pct = safe_float(row["raw_pct"])
        if (
            not np.isfinite(op) or not np.isfinite(close) or not np.isfinite(pct)
            or close <= op or pct <= 0 or pct > MAX_DAILY_GAIN
            or bool(row["suspected_action"])
        ):
            break
        streak += 1
        start_idx = i
    return streak, start_idx


def check_support(df, surge_idx, code, name):
    """启动日之后，最低价不能跌破启动日前一日收盘价对应的理论涨停价。"""
    if surge_idx <= 0 or surge_idx >= len(df):
        return False, np.nan, np.nan
    previous_close = safe_float(df.iloc[surge_idx - 1]["close"])
    limit_price = theoretical_limit_up(previous_close, code, name)
    if not np.isfinite(limit_price):
        return False, limit_price, np.nan
    after_surge = df.iloc[surge_idx + 1:]
    if after_surge.empty:
        return False, limit_price, np.nan
    lowest_after = safe_float(after_surge["low"].min())
    return (
        np.isfinite(lowest_after) and lowest_after >= limit_price,
        limit_price,
        lowest_after,
    )


def analyze_stock(stock, start_date, end_date):
    code = normalize_code(stock["code"])
    name = str(stock["name"]).strip()
    try:
        df = fetch_daily_history(code, start_date, end_date)
        if df.empty or len(df) < MIN_HISTORY:
            return None
        df = prepare_market_data(df)

        position_ok, position_ratio, position_low, position_high = check_low_position(df)
        if not position_ok:
            return None

        bullish_streak, bullish_start_idx = get_current_bullish_streak(df)
        if bullish_streak < MIN_BULLISH_DAYS or bullish_start_idx is None:
            return None

        surge_days = find_surge_days(df)
        if not surge_days:
            return None

        for surge_idx in reversed(surge_days):
            support_ok, support_price, lowest_after = check_support(
                df, surge_idx, code, name
            )
            if not support_ok:
                continue

            gaps = find_confirmed_gaps(df, surge_idx)
            valid_gaps = [
                g for g in gaps
                if surge_idx < g["gap_idx"] < g["confirm_idx"] < bullish_start_idx
            ]
            if not valid_gaps:
                continue

            gap = max(valid_gaps, key=lambda item: item["gap_idx"])
            latest = df.iloc[-1]
            surge_row = df.iloc[surge_idx]
            gap_row = df.iloc[gap["gap_idx"]]
            return {
                "代码": code,
                "名称": name,
                "最新交易日": latest["date"].strftime("%Y-%m-%d"),
                "最新收盘价": round(safe_float(latest["close"]), 3),
                "连续真阳线数量": bullish_streak,
                "连续阳线起始日": df.iloc[bullish_start_idx]["date"].strftime("%Y-%m-%d"),
                "启动日": surge_row["date"].strftime("%Y-%m-%d"),
                "启动日涨幅_pct": round(safe_float(surge_row["raw_pct"]), 3),
                "启动日成交量": safe_float(surge_row["volume"]),
                "启动日理论涨停价": support_price,
                "启动日后最低价": lowest_after,
                "跳空日": gap_row["date"].strftime("%Y-%m-%d"),
                "缺口下沿": gap["gap_lower"],
                "缺口上沿": gap["gap_upper"],
                "缺口确认完成日": df.iloc[gap["confirm_idx"]]["date"].strftime("%Y-%m-%d"),
                "250日最低价": position_low,
                "250日最高价": position_high,
                "250日区间位置_pct": round(position_ratio * 100, 3),
            }
        return None
    except Exception:
        logger.exception("%s %s 筛选异常", code, name)
        return None


def save_html_report(result_df, output_path=HTML_REPORT):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    report_df = result_df.copy()
    for col in RESULT_COLUMNS:
        if col not in report_df.columns:
            report_df[col] = pd.Series(dtype="object")
    report_df = report_df[RESULT_COLUMNS]

    # 生成带超链接与样式的高级表格行
    rows_html = []
    all_codes = []
    for _, row in report_df.iterrows():
        code = str(row["代码"])
        all_codes.append(code)
        market = get_market_prefix(code)
        stock_url = f"https://quote.eastmoney.com/{market}{code}.html"
        code_cell = f'<a class="stock-link" href="{stock_url}" target="_blank" title="打开东财行情">{code}</a>'
        name_cell = f'<strong>{row["名称"]}</strong>'

        cells = [
            f'<td>{code_cell}</td>',
            f'<td>{name_cell}</td>',
            f'<td>{row["最新交易日"]}</td>',
            f'<td>{row["最新收盘价"]:.2f}</td>',
            f'<td><span class="badge badge-bullish">{row["连续真阳线数量"]} 连阳</span></td>',
            f'<td>{row["连续阳线起始日"]}</td>',
            f'<td>{row["启动日"]}</td>',
            f'<td class="text-up">+{row["启动日涨幅_pct"]:.2f}%</td>',
            f'<td>{int(row["启动日成交量"]):,}</td>',
            f'<td>{row["启动日理论涨停价"]:.2f}</td>',
            f'<td>{row["启动日后最低价"]:.2f}</td>',
            f'<td>{row["跳空日"]}</td>',
            f'<td>{row["缺口下沿"]:.2f}</td>',
            f'<td>{row["缺口上沿"]:.2f}</td>',
            f'<td>{row["缺口确认完成日"]}</td>',
            f'<td>{row["250日最低价"]:.2f}</td>',
            f'<td>{row["250日最高价"]:.2f}</td>',
            f'<td><span class="badge badge-pos">{row["250日区间位置_pct"]:.1f}%</span></td>',
        ]
        rows_html.append(f'<tr>{"".join(cells)}</tr>')

    tbody_content = "\n".join(rows_html)
    thead_ths = "".join(f'<th data-col="{i}">{col} <span class="sort-icon">⇅</span></th>' for i, col in enumerate(RESULT_COLUMNS))

    beijing_now = get_beijing_now()
    generated_at = beijing_now.strftime("%Y-%m-%d %H:%M:%S (北京时间)")
    latest_date = str(report_df["最新交易日"].iloc[0]) if not report_df.empty else "暂无候选"
    count = len(report_df)
    codes_json_str = ",".join(all_codes)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股智能选股报告 - 每日更新</title>
<style>
:root {{
  --bg: #0f172a;
  --panel: #1e293b;
  --card-bg: #1e293b;
  --text: #f8fafc;
  --text-muted: #94a3b8;
  --border: #334155;
  --accent: #38bdf8;
  --accent-hover: #0284c7;
  --up: #f43f5e;
  --badge-bullish-bg: rgba(244, 63, 94, 0.15);
  --badge-bullish-text: #fb7185;
  --badge-pos-bg: rgba(56, 189, 248, 0.15);
  --badge-pos-text: #38bdf8;
  --hover-tr: #2d3748;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
  padding: 24px 20px;
  line-height: 1.5;
}}
.container {{ max-width: 1680px; margin: 0 auto; }}

/* 头部 */
header {{
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--border);
  gap: 16px;
}}
.title-group h1 {{
  font-size: 26px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -0.5px;
}}
.title-group p {{
  color: var(--text-muted);
  font-size: 13px;
  margin-top: 6px;
}}

/* 概览卡片 */
.stats-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin: 24px 0;
}}
.stat-card {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 20px;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}}
.stat-label {{
  font-size: 13px;
  color: var(--text-muted);
}}
.stat-value {{
  font-size: 28px;
  font-weight: 700;
  margin-top: 8px;
  color: #fff;
}}
.stat-value.highlight {{
  color: var(--accent);
}}
.stat-value.count-up {{
  color: var(--up);
}}

/* 控制栏 */
.toolbar {{
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px 12px 0 0;
  padding: 16px 20px;
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  border-bottom: none;
}}
.search-box {{
  position: relative;
  min-width: 280px;
}}
.search-box input {{
  width: 100%;
  padding: 9px 14px;
  border-radius: 8px;
  background: #0f172a;
  border: 1px solid var(--border);
  color: #fff;
  font-size: 14px;
  outline: none;
  transition: border-color 0.2s;
}}
.search-box input:focus {{
  border-color: var(--accent);
}}
.action-buttons {{
  display: flex;
  gap: 10px;
}}
.btn {{
  background: #334155;
  color: #fff;
  border: 1px solid #475569;
  border-radius: 8px;
  padding: 8px 14px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
}}
.btn:hover {{
  background: #475569;
}}
.btn-primary {{
  background: #0284c7;
  border-color: #0369a1;
}}
.btn-primary:hover {{
  background: #0369a1;
}}

/* 表格容器 */
.table-container {{
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 0 0 12px 12px;
  overflow-x: auto;
}}
table.stock-table {{
  width: 100%;
  border-collapse: collapse;
  white-space: nowrap;
  font-size: 13px;
}}
table.stock-table th {{
  background: #1e293b;
  color: var(--text-muted);
  font-weight: 600;
  text-align: left;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  user-select: none;
}}
table.stock-table th:hover {{
  color: #fff;
  background: #283548;
}}
table.stock-table td {{
  padding: 13px 16px;
  border-bottom: 1px solid #283548;
  color: #e2e8f0;
}}
table.stock-table tbody tr:hover {{
  background: var(--hover-tr);
}}
.stock-link {{
  color: var(--accent);
  text-decoration: none;
  font-weight: 600;
  font-family: monospace;
  font-size: 14px;
}}
.stock-link:hover {{
  text-decoration: underline;
  color: #7dd3fc;
}}
.text-up {{
  color: var(--up);
  font-weight: 600;
}}
.badge {{
  display: inline-block;
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
}}
.badge-bullish {{
  background: var(--badge-bullish-bg);
  color: var(--badge-bullish-text);
}}
.badge-pos {{
  background: var(--badge-pos-bg);
  color: var(--badge-pos-text);
}}
.sort-icon {{
  font-size: 10px;
  margin-left: 4px;
  color: #64748b;
}}

/* 页脚说明 */
.footer-note {{
  margin-top: 24px;
  padding: 16px 20px;
  border-radius: 8px;
  background: #1e293b;
  border-left: 4px solid var(--accent);
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.8;
}}

/* 空状态 */
.empty-state {{
  text-align: center;
  padding: 48px 0;
  color: var(--text-muted);
  font-size: 15px;
}}

@media (max-width: 768px) {{
  body {{ padding: 14px 10px; }}
  .title-group h1 {{ font-size: 20px; }}
  .toolbar {{ flex-direction: column; align-items: stretch; }}
  .search-box {{ width: 100%; }}
  .action-buttons {{ justify-content: flex-start; }}
}}
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="title-group">
      <h1>📈 A股每日选股策略报告</h1>
      <p>执行时间：{generated_at} ｜ 行情基准日：{latest_date}</p>
    </div>
    <div class="action-buttons">
      <button class="btn" id="copyCodesBtn" onclick="copyStockCodes()">📋 复制代码列表</button>
      <a class="btn btn-primary" href="stock_candidates.csv" download>📥 下载 CSV 数据</a>
    </div>
  </header>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">今日入选股票</div>
      <div class="stat-value count-up">{count} <span style="font-size:14px;font-weight:normal;color:#94a3b8">只</span></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">250日价格位置门槛</div>
      <div class="stat-value highlight">&lt; {MAX_POSITION * 100:.0f}%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">连续真阳线门槛</div>
      <div class="stat-value">&ge; {MIN_BULLISH_DAYS} 根</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">启动日单日放量</div>
      <div class="stat-value">&ge; {VOLUME_MULTIPLE:.1f} 倍</div>
    </div>
  </div>

  <div class="toolbar">
    <div class="search-box">
      <input type="search" id="searchInput" placeholder="搜索代码、股票名称..." autocomplete="off">
    </div>
    <div style="font-size: 13px; color: var(--text-muted)">
      点击表头可双向排序 ｜ 点击代码直接跳转东财行情
    </div>
  </div>

  <div class="table-container">
    <table class="stock-table" id="stockTable">
      <thead>
        <tr>{thead_ths}</tr>
      </thead>
      <tbody>
        {tbody_content if count > 0 else '<tr><td colspan="18" class="empty-state">今日暂无符合全部条件的候选标的</td></tr>'}
      </tbody>
    </table>
  </div>

  <div class="footer-note">
    <strong>📌 筛选逻辑说明：</strong><br>
    1. <strong>位置过滤</strong>：严格限制最新价在近250个交易日极值区间的 50% 以下（低位安全边际）。<br>
    2. <strong>形态要求</strong>：过去 20 个交易日内出现过单日涨幅 &ge; 10% 且放量 1.5 倍以上的启动日；后续出现过有效跳空缺口（3日内不回补）；启动日后未跌破理论涨停底线。<br>
    3. <strong>温和连阳</strong>：截至最新收盘日，维持至少连续 3 根涨幅在 (0, 5%] 范围内的真阳线。<br>
    ⚠️ <em>免责声明：本工具为量化策略筛选实验，数据源自公开行情接口，不构成任何投资建议，股市有风险，入市需谨慎。</em>
  </div>
</div>

<script>
const STOCK_CODES = "{codes_json_str}";

function copyStockCodes() {{
  if (!STOCK_CODES) {{
    alert("当前无入选股票代码");
    return;
  }}
  const formatted = STOCK_CODES.split(",").join("\\n");
  navigator.clipboard.writeText(formatted).then(() => {{
    const btn = document.getElementById("copyCodesBtn");
    const oldText = btn.innerText;
    btn.innerText = "✅ 已复制到剪贴板";
    setTimeout(() => btn.innerText = oldText, 2000);
  }}).catch(err => {{
    alert("复制失败，请手动选取复制");
  }});
}}

(function() {{
  const input = document.getElementById("searchInput");
  const table = document.getElementById("stockTable");
  if (!table) return;
  const tbody = table.tBodies[0];

  if (input) {{
    input.addEventListener("input", function() {{
      const q = input.value.trim().toLowerCase();
      Array.from(tbody.rows).forEach(row => {{
        row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
      }});
    }});
  }}

  // 表头排序
  Array.from(table.tHead.rows[0].cells).forEach((th, index) => {{
    let asc = true;
    th.addEventListener("click", function() {{
      const rows = Array.from(tbody.rows);
      if (rows.length <= 1 && rows[0].cells.length === 1) return; // 空状态行跳过
      rows.sort((a, b) => {{
        const x = a.cells[index].innerText.replace(/[%+,]/g, "").trim();
        const y = b.cells[index].innerText.replace(/[%+,]/g, "").trim();
        const nx = parseFloat(x);
        const ny = parseFloat(y);
        if (!isNaN(nx) && !isNaN(ny)) {{
          return asc ? nx - ny : ny - nx;
        }}
        return asc ? x.localeCompare(y, "zh-CN") : y.localeCompare(x, "zh-CN");
      }});
      rows.forEach(r => tbody.appendChild(r));
      asc = !asc;
    }});
  }});
}})();
</script>
</body>
</html>"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("HTML 报告已生成：%s", os.path.abspath(output_path))


def main():
    start_time = time.time()
    beijing_start = get_beijing_now()
    logger.info("=" * 60)
    logger.info("A股智能选股开始（执行时间：%s）", beijing_start.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("参数配置：区间位置 < %.1f%%，连阳 >= %d 根，仅主板=%s，排除ST=%s，排除退市=%s",
                MAX_POSITION * 100, MIN_BULLISH_DAYS, MAIN_BOARD_ONLY, EXCLUDE_ST, EXCLUDE_DELISTING)

    today = beijing_start.date()
    start_date = (today - dt.timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    # 获取股票初筛池（两阶段极速模式）
    stocks = get_stock_pool()

    results = []
    total = len(stocks)
    completed = 0
    logger.info("开始拉取并回测历史数据，待精析股票数：%d", total)

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(analyze_stock, row, start_date, end_date): row
            for _, row in stocks.iterrows()
        }
        for future in as_completed(futures):
            completed += 1
            stock = futures[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
                    logger.info("🎉 命中候选：%s %s", result["代码"], result["名称"])
            except Exception:
                logger.exception("处理股票失败：%s %s", stock.get("code", ""), stock.get("name", ""))
            if completed % 50 == 0 or completed == total:
                logger.info("精析进度：%d/%d (%.1f%%)", completed, total, (completed / total) * 100)

    result_df = pd.DataFrame(results, columns=RESULT_COLUMNS)
    if not result_df.empty:
        result_df = result_df.sort_values(
            by=["250日区间位置_pct", "连续真阳线数量"],
            ascending=[True, False],
        ).reset_index(drop=True)

    result_df.to_csv(RESULT_CSV, index=False, encoding="utf-8-sig")
    save_html_report(result_df, HTML_REPORT)

    logger.info("筛选全部完成！最终命中候选股票：%d 只", len(result_df))
    logger.info("CSV 文件输出：%s", os.path.abspath(RESULT_CSV))
    logger.info("HTML 网页输出：%s", os.path.abspath(HTML_REPORT))
    logger.info("总耗时：%.1f 秒", time.time() - start_time)
    print(result_df.to_string(index=False) if not result_df.empty else "本次没有符合条件的股票。")


if __name__ == "__main__":
    main()
