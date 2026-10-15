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
RETRY_TIMES = 2

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


def safe_float(value, default=np.nan):
    try:
        value = float(value)
        return value if np.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def normalize_code(code):
    return str(code).strip().zfill(6)


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


def get_stock_list():
    logger.info("开始获取 A 股股票列表")
    stocks = ak.stock_info_a_code_name()
    if stocks is None or stocks.empty:
        raise RuntimeError("获取股票列表失败：接口返回空数据")

    stocks = stocks.copy()
    stocks.columns = [str(c).strip() for c in stocks.columns]
    if "code" not in stocks.columns or "name" not in stocks.columns:
        raise RuntimeError(f"股票列表字段异常：{stocks.columns.tolist()}")

    stocks["code"] = stocks["code"].map(normalize_code)
    stocks["name"] = stocks["name"].astype(str).str.strip()
    stocks = stocks[stocks["code"].str.fullmatch(r"\d{6}", na=False)].copy()

    if EXCLUDE_ST:
        stocks = stocks[~stocks["name"].str.contains(r"ST", case=False, na=False)]
    if EXCLUDE_DELISTING:
        stocks = stocks[~stocks["name"].str.contains(r"退", case=False, na=False)]
    if MAIN_BOARD_ONLY:
        stocks = stocks[stocks["code"].map(is_main_board)]

    stocks = stocks.drop_duplicates(subset=["code"]).sort_values("code")
    stocks = stocks.reset_index(drop=True)
    logger.info("待筛选股票数量：%d", len(stocks))
    return stocks


def fetch_daily_history(code, start_date, end_date):
    last_error = None
    for attempt in range(RETRY_TIMES + 1):
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
            logger.warning(
                "%s 行情获取失败（尝试 %d/%d）：%s",
                code, attempt + 1, RETRY_TIMES + 1, exc,
            )
            time.sleep(0.5 * (attempt + 1))

    logger.error("%s 行情获取最终失败：%s", code, last_error, exc_info=True)
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

    table_html = report_df.to_html(
        index=False, escape=True, border=0,
        classes="stock-table", na_rep="", table_id="stockTable",
        float_format=lambda x: f"{x:.3f}",
    )
    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    latest_date = (
        str(report_df["最新交易日"].iloc[0]) if not report_df.empty else "暂无候选"
    )
    count = len(report_df)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股选股报告</title>
<style>
:root {{ color-scheme: light; --bg:#f4f6fa; --panel:#fff; --text:#202938; --muted:#667085; --line:#e4e7ec; --accent:#2563eb; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
.container {{ max-width:1600px; margin:0 auto; }}
h1 {{ margin:0 0 8px; font-size:28px; }}
.subtitle {{ color:var(--muted); font-size:14px; line-height:1.8; }}
.summary {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; margin:20px 0; }}
.card,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
.card {{ padding:20px; }}
.card-label {{ color:var(--muted); font-size:13px; }}
.card-value {{ margin-top:8px; font-size:25px; font-weight:700; }}
.panel {{ padding:20px; }}
.toolbar {{ display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:12px; margin-bottom:16px; }}
input {{ width:min(100%,360px); padding:10px 12px; border:1px solid #d0d5dd; border-radius:8px; font-size:14px; }}
.table-wrap {{ width:100%; overflow-x:auto; }}
.stock-table {{ width:100%; border-collapse:collapse; white-space:nowrap; font-size:13px; }}
.stock-table th,.stock-table td {{ padding:12px 14px; border-bottom:1px solid var(--line); text-align:left; }}
.stock-table th {{ position:sticky; top:0; background:#f8fafc; cursor:pointer; user-select:none; }}
.stock-table th:hover {{ color:var(--accent); }}
.stock-table tbody tr:hover {{ background:#f8fafc; }}
.note {{ margin-top:18px; color:var(--muted); font-size:12px; line-height:1.8; }}
@media(max-width:700px) {{ body{{padding:12px}} h1{{font-size:22px}} .summary{{grid-template-columns:1fr;gap:10px}} .card,.panel{{padding:14px}} }}
</style>
</head>
<body>
<div class="container">
<header>
<h1>A股选股报告</h1>
<div class="subtitle">报告生成时间：{generated_at}<br>最新行情日期：{latest_date}</div>
</header>
<section class="summary">
<div class="card"><div class="card-label">候选股票数量</div><div class="card-value">{count}</div></div>
<div class="card"><div class="card-label">250日区间位置</div><div class="card-value">严格低于 {MAX_POSITION * 100:.0f}%</div></div>
<div class="card"><div class="card-label">最低连续真阳线</div><div class="card-value">{MIN_BULLISH_DAYS} 根</div></div>
</section>
<section class="panel">
<div class="toolbar"><h2 style="margin:0;font-size:18px">候选股票明细</h2>
<input id="searchInput" type="search" placeholder="搜索股票代码或名称..." aria-label="搜索股票代码或名称"></div>
<div class="table-wrap">{table_html}</div>
<div class="note">
价格区间位置 = (当前收盘价 - 近250日最低价) / (近250日最高价 - 近250日最低价) × 100%。<br>
启动日理论涨停价为估算值，不代表历史交易所实际涨停价。疑似除权除息识别为启发式过滤，不能保证识别全部公司行为。报告仅供研究参考，不构成投资建议。
</div>
</section>
</div>
<script>
(function() {{
 const input=document.getElementById("searchInput");
 const table=document.getElementById("stockTable");
 if(!input||!table) return;
 const tbody=table.tBodies[0];
 input.addEventListener("input",function() {{
   const q=input.value.trim().toLowerCase();
   Array.from(tbody.rows).forEach(row=>row.style.display=row.textContent.toLowerCase().includes(q)?"":"none");
 }});
 Array.from(table.tHead.rows[0].cells).forEach((th,index)=>{{
   let asc=true; th.title="点击排序";
   th.addEventListener("click",function() {{
     const rows=Array.from(tbody.rows);
     rows.sort((a,b)=>{{
       const x=a.cells[index].textContent.trim(), y=b.cells[index].textContent.trim();
       const nx=Number(x), ny=Number(y);
       const result=(x!==""&&y!==""&&Number.isFinite(nx)&&Number.isFinite(ny))
         ? nx-ny : x.localeCompare(y,"zh-CN",{{numeric:true}});
       return asc?result:-result;
     }});
     rows.forEach(row=>tbody.appendChild(row)); asc=!asc;
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
    logger.info("=" * 60)
    logger.info("A股选股开始；250日区间位置严格小于 %.1f%%", MAX_POSITION * 100)
    logger.info("仅主板：%s；排除ST：%s；排除退市：%s",
                MAIN_BOARD_ONLY, EXCLUDE_ST, EXCLUDE_DELISTING)

    today = dt.date.today()
    start_date = (today - dt.timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")
    stocks = get_stock_list()

    results = []
    total = len(stocks)
    completed = 0
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
                    logger.info("命中候选：%s %s", result["代码"], result["名称"])
            except Exception:
                logger.exception("处理股票失败：%s %s", stock.get("code", ""), stock.get("name", ""))
            if completed % 100 == 0 or completed == total:
                logger.info("进度：%d/%d", completed, total)

    result_df = pd.DataFrame(results, columns=RESULT_COLUMNS)
    if not result_df.empty:
        result_df = result_df.sort_values(
            by=["250日区间位置_pct", "连续真阳线数量"],
            ascending=[True, False],
        ).reset_index(drop=True)

    result_df.to_csv(RESULT_CSV, index=False, encoding="utf-8-sig")
    save_html_report(result_df, HTML_REPORT)

    logger.info("筛选完成，候选数量：%d", len(result_df))
    logger.info("CSV：%s", os.path.abspath(RESULT_CSV))
    logger.info("HTML：%s", os.path.abspath(HTML_REPORT))
    logger.info("耗时：%.1f秒", time.time() - start_time)
    print(result_df.to_string(index=False) if not result_df.empty else "本次没有符合条件的股票。")


if __name__ == "__main__":
    main()
