#!/usr/bin/env python3
"""
恐慌情緒進場訊號系統  Fear-Signal
=================================================
分析 CNN 恐懼貪婪指數 (Fear & Greed)、MOVE 指數 (美債波動率)、VIX 指數 (股市恐慌指數)，
以「相對比較」(滾動 252 日百分位) 合成「綜合恐慌分數」，
統計同時期往後一個月 (21 個交易日) S&P 500、納指 100、費城半導體的報酬，
並輸出：SQLite 資料庫、CSV、signal.json、手機版燈號網頁 docs/index.html。

用法：
    python fear_signal.py            # 抓最新資料、重建資料庫、更新燈號
    python fear_signal.py --years 10 # 指定回測年數 (預設 10)

資料來源 (依優先順序，失敗自動換下一個)：
    F&G  : CNN 官方 API  → GitHub 鏡像 (whit3rabbit/fear-greed-data)
    VIX  : CBOE 官方 CSV → Yahoo ^VIX → GitHub 鏡像
    MOVE : Yahoo ^MOVE   → GitHub 鏡像 (kittycapital/move_index)
    指數 : Yahoo ^GSPC / ^NDX / ^SOX → GitHub 鏡像 (^GSPC / QQQ 代理 / ^SOX)
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# ---------------------------------------------------------------- 參數設定
FWD_DAYS = 21          # 一個月 = 21 個交易日 (相似時點排除用)
HORIZONS = {"2w": (10, "2週"), "1m": (21, "1個月"), "3m": (63, "3個月")}   # 觀察期間 (交易日)
PCT_WINDOW = 252       # 相對比較：過去一年 (252 交易日) 的百分位
WEIGHTS = {"fg": 1 / 3, "vix": 1 / 3, "move": 1 / 3}   # 綜合恐慌分數權重

# 燈號規則（依過去 10 年回測結果設定，取整數門檻以降低過度配適）
#   綠燈：股市「絕對」恐慌到位 → (F&G ≤ 25 且 VIX ≥ 25) 或 VIX ≥ 30
#   黃燈：恐慌升溫但未到位 → 綜合恐慌分數 ≥ 60、或 F&G ≤ 25、或 VIX ≥ 25
#   灰燈：無明顯優勢，維持原有配置
GREEN_FG, GREEN_VIX, GREEN_VIX_ALONE = 25, 25, 30
YELLOW_SCORE = 60

# ---- 新增評估指標（總經風險）
# VXN：那斯達克100 波動率；VIX÷VIX3M：期限結構 (>1 = 倒掛，短期恐慌高於中期)
# HY：ICE BofA 美國高收益債利差 (OAS, %)；RR：10 年期抗通膨公債實質殖利率 (%)
MACRO_SRC = {
    "vxn":   {"yahoo": "^VXN",   "fred": "VXNCLS",
              "mirror": "https://raw.githubusercontent.com/achl0902-web/back-testing/main/qld/data/macro/VXNCLS.csv"},
    "vix3m": {"yahoo": "^VIX3M", "fred": "VXVCLS",
              "mirror": "https://raw.githubusercontent.com/achl0902-web/back-testing/main/qld/data/macro/VXVCLS.csv"},
    "hy":    {"yahoo": None, "fred": "BAMLH0A0HYM2",
              "mirror": "https://raw.githubusercontent.com/jini-bot/soxx-data/main/fred_hyspread_raw.csv"},
    "rr":    {"yahoo": None, "fred": "DFII10",
              "mirror": "https://raw.githubusercontent.com/ethanteng/finsight/main/src/datasets/DFII10.csv"},
}
MACRO_ZH = {"vxn": "VXN", "vix3m": "VIX3M", "hy": "高收益債利差", "rr": "10年實質利率"}
SEED_MACRO = Path(__file__).resolve().parent / "data" / "seed_macro.csv"   # 歷史種子 (FRED 的 HY 只開放近 3 年)

# 確認分數 (綠燈時使用；回測依據見 PDF「新增指標」章節)
#   +1 VIX÷VIX3M 連續倒掛 ≥ 3 日   +1 VXN ≥ 40   +1 高收益債利差 ≥ 5%（信用面恐慌出清）
#   −1 高收益債利差 < 5% 但 5 日擴大 ≥ 0.3（信用惡化中、尚未出清）
INV_DAYS_MIN, VXN_HI, HY_CAPIT, HY_WIDEN = 3, 40, 5.0, 0.3
TS_EXTREME, HY_CRISIS_LVL, HY_CRISIS_5D, RR_HEADWIND = 1.10, 4.5, 0.5, 2.5
# 過熱紅燈（退場警示）：F&G ≥ 75 且（VIX < 15 或 高收益債利差在一年內的百分位 ≤ 20，即信用面過度樂觀）
#   深紅（強烈警示）：再加上 F&G ≥ 75 連續 ≥ 10 日，或 F&G ≥ 85
RED_FG, RED_VIX, RED_HY_PCT, RED_STREAK, RED_FG_EXTREME = 75, 15, 20, 10, 85
# 進場時點：綠燈第一天先等待，綜合分數自（綠燈後的）最高點回落 ≥ TURN_DROP 分，才發出第 1 批進場訊號
#   之後須「再度出現綠燈並創新高 → 再回落 ≥ TURN_DROP」才發出下一批，每段最多 MAX_TRANCHES 批
#   段落結束：最後一個綠燈日後 EPISODE_TAIL 個交易日內沒有新綠燈
TURN_DROP, MAX_TRANCHES, EPISODE_TAIL = 5.0, 3, 20
# 重大事件：進場訊號日的「下一個交易日」若有高重要度事件（FOMC／CPI／非農），改在事件當日收盤（公布後）執行
EVENTS_FILE = Path(__file__).resolve().parent / "data" / "event_calendar.csv"
# 機會資金動用比例 (綠燈時，依確認分數)
DEPLOY = {"conf<=0": 0.20, "conf=1": 0.33, "conf>=2": 0.50}

INDICES = {"spx": "S&P 500", "ndx": "納指100", "sox": "費城半導體"}

MIRROR = "https://raw.githubusercontent.com"
SRC = {
    "fg_cnn": "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}",
    "fg_mirror": f"{MIRROR}/whit3rabbit/fear-greed-data/main/fear-greed.csv",
    "vix_cboe": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "move_mirror": f"{MIRROR}/kittycapital/move_index/main/data/move_dashboard.json",
    "px_mirror": f"{MIRROR}/jini-bot/soxx-data/main/{{file}}",
}
YAHOO = {"vix": "^VIX", "move": "^MOVE", "spx": "^GSPC", "ndx": "^NDX", "sox": "^SOX"}
PX_MIRROR_FILE = {"spx": "ref_gspc_raw.csv", "ndx": "qqq_raw.csv", "sox": "ref_sox_raw.csv",
                  "vix": "ref_vix_raw.csv"}

SOURCES_USED: dict[str, str] = {}
DATA_DATES: dict[str, str] = {}     # 各序列實際最新資料日 (未 forward-fill 前)
MANUAL = DATA / "manual_overrides.csv"   # 手動補值：date,series,value  (series = fg / vix / move)


def apply_manual(key: str, s: pd.Series) -> pd.Series:
    """若 data/manual_overrides.csv 有該序列的手動數值，以手動值優先 (補官方來源尚未更新的日期)。"""
    if not MANUAL.exists():
        return s
    m = pd.read_csv(MANUAL, comment="#")
    m = m[m["series"].str.strip().str.lower() == key]
    if m.empty:
        return s
    ms = _clean(pd.Series(m["value"].values, index=pd.to_datetime(m["date"])), positive=key not in ("rr", "hy"))
    new_dates = ms.index.difference(s.index)
    if len(new_dates):
        SOURCES_USED[key] = SOURCES_USED.get(key, "") + f" + 手動補值 {len(new_dates)} 筆"
    return ms.combine_first(s)


# ---------------------------------------------------------------- 抓資料
def _get(url: str, timeout: int = 30) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _clean(s: pd.Series, positive: bool = True) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s > 0] if positive else s       # 實質利率可能為負，不能過濾


def yahoo(ticker: str, start: str) -> pd.Series:
    import yfinance as yf
    df = yf.download(ticker, start=start, progress=False, auto_adjust=False, threads=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo 無資料 {ticker}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return _clean(close)


def mirror_px(key: str) -> pd.Series:
    txt = _get(SRC["px_mirror"].format(file=PX_MIRROR_FILE[key])).text.lstrip("﻿")
    df = pd.read_csv(io.StringIO(txt), index_col="Date")
    return _clean(df["Close"])


def fetch_fg(start: str) -> pd.Series:
    parts = []
    try:
        df = pd.read_csv(io.StringIO(_get(SRC["fg_mirror"]).text), index_col="Date")
        parts.append(("GitHub 鏡像", _clean(df["Fear Greed"])))
    except Exception as e:  # noqa: BLE001
        print("  F&G 鏡像失敗:", e)
    try:
        j = _get(SRC["fg_cnn"].format(start=start)).json()
        pts = j["fear_and_greed_historical"]["data"]
        s = pd.Series({pd.to_datetime(p["x"], unit="ms"): p["y"] for p in pts})
        cur = j.get("fear_and_greed", {})
        if cur.get("score") is not None and cur.get("timestamp"):
            s[pd.to_datetime(cur["timestamp"]).tz_localize(None)] = cur["score"]
        parts.append(("CNN 官方", _clean(s)))
    except Exception as e:  # noqa: BLE001
        print("  F&G CNN 失敗:", e)
    if not parts:
        raise RuntimeError("F&G 所有來源皆失敗")
    out = parts[0][1]
    for _, s in parts[1:]:
        out = s.combine_first(out)          # 後者 (CNN 官方) 優先
    SOURCES_USED["fg"] = " + ".join(n for n, _ in parts)
    return out


def fetch_vix(start: str) -> pd.Series:
    try:
        df = pd.read_csv(io.StringIO(_get(SRC["vix_cboe"]).text))
        s = pd.Series(df["CLOSE"].values, index=pd.to_datetime(df["DATE"], format="%m/%d/%Y"))
        SOURCES_USED["vix"] = "CBOE 官方"
        return _clean(s)
    except Exception as e:  # noqa: BLE001
        print("  VIX CBOE 失敗:", e)
    for name, fn in (("Yahoo ^VIX", lambda: yahoo("^VIX", start)),
                     ("GitHub 鏡像", lambda: mirror_px("vix"))):
        try:
            s = fn()
            SOURCES_USED["vix"] = name
            return s
        except Exception as e:  # noqa: BLE001
            print(f"  VIX {name} 失敗:", e)
    raise RuntimeError("VIX 所有來源皆失敗")


def fetch_move(start: str) -> pd.Series:
    try:
        s = yahoo("^MOVE", start)
        SOURCES_USED["move"] = "Yahoo ^MOVE"
        return s
    except Exception as e:  # noqa: BLE001
        print("  MOVE Yahoo 失敗:", e)
    j = _get(SRC["move_mirror"]).json()["series"]["MOVE"]
    s = pd.Series({pd.to_datetime(t, unit="ms"): v for t, v in j})
    SOURCES_USED["move"] = "GitHub 鏡像"
    return _clean(s)


def fetch_index(key: str, start: str) -> pd.Series:
    # 同一指數不混用不同來源，避免點位不一致造成假報酬
    try:
        s = yahoo(YAHOO[key], start)
        SOURCES_USED[key] = f"Yahoo {YAHOO[key]}"
        return s
    except Exception as e:  # noqa: BLE001
        print(f"  {key} Yahoo 失敗:", e)
    s = mirror_px(key)
    SOURCES_USED[key] = "GitHub 鏡像" + ("（QQQ 價格代理）" if key == "ndx" else "")
    return s


def fred(series_id: str, start: str) -> pd.Series:
    txt = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}").text
    df = pd.read_csv(io.StringIO(txt))
    return _clean(pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                            index=pd.to_datetime(df.iloc[:, 0])), positive=False)


def treasury_real10(start: str) -> pd.Series:
    """美國財政部官方實質殖利率曲線 10 年期（FRED DFII10 的原始來源）。"""
    parts = []
    for yr in range(int(start[:4]), pd.Timestamp.today().year + 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{yr}/all?type=daily_treasury_real_yield_curve&field_tdr_date_value={yr}&page&_format=csv")
        d = pd.read_csv(io.StringIO(_get(url).text))
        parts.append(pd.Series(pd.to_numeric(d["10 YR"], errors="coerce").values, index=pd.to_datetime(d["Date"])))
    return _clean(pd.concat(parts), positive=False)


def fetch_macro(key: str, start: str) -> pd.Series:
    """新增指標：Yahoo → FRED → GitHub 鏡像，並以 data/seed_macro.csv 補足較早歷史。"""
    cfg, got, name = MACRO_SRC[key], None, None
    tries = []
    if cfg["yahoo"]:
        tries.append((f"Yahoo {cfg['yahoo']}", lambda: yahoo(cfg["yahoo"], start)))
    tries.append((f"FRED {cfg['fred']}", lambda: fred(cfg["fred"], start)))
    if key == "rr":
        tries.append(("美國財政部 實質殖利率曲線", lambda: treasury_real10(start)))
    tries.append(("GitHub 鏡像", lambda: _clean(pd.read_csv(io.StringIO(_get(cfg["mirror"]).text.lstrip("\ufeff")),
                                                            index_col=0).iloc[:, -1], positive=False)))
    for n, fn in tries:
        try:
            got, name = fn(), n
            if len(got):
                break
        except Exception as e:  # noqa: BLE001
            print(f"  {key} {n} 失敗:", e)
    seed = pd.Series(dtype=float)
    if SEED_MACRO.exists():
        sd = pd.read_csv(SEED_MACRO, index_col=0, parse_dates=True)
        if key in sd:
            seed = _clean(sd[key], positive=False)
    if got is None or got.empty:
        SOURCES_USED[key] = "歷史種子檔（未能取得最新資料）"
        return seed
    tag = []
    if len(seed) and seed.index.min() < got.index.min():
        tag.append("歷史種子")
    if len(seed) and seed.index.max() > got.index.max():
        tag.append(f"種子補至 {seed.index.max():%m-%d}")
    SOURCES_USED[key] = name + (" + " + "、".join(tag) if tag else "")
    return got.combine_first(seed)


# ---------------------------------------------------------------- 建資料庫
def rolling_pct(s: pd.Series, win: int = PCT_WINDOW) -> pd.Series:
    """今日數值在過去 win 日中的百分位 (0-100)，只用當時已知資料，無前視偏差。"""
    return s.rolling(win, min_periods=int(win * 0.8)).apply(
        lambda a: (a[:-1] < a[-1]).mean() * 100 + (a[:-1] == a[-1]).mean() * 50, raw=True)


def build_panel(years: int) -> pd.DataFrame:
    today = pd.Timestamp.today().normalize()
    start_analysis = today - pd.DateOffset(years=years)
    start_fetch = (start_analysis - pd.DateOffset(years=2)).strftime("%Y-%m-%d")

    print("抓取資料中…")
    px = {k: fetch_index(k, start_fetch) for k in INDICES}
    fg, vix, move = fetch_fg(start_fetch), fetch_vix(start_fetch), fetch_move(start_fetch)
    fg, vix, move = apply_manual("fg", fg), apply_manual("vix", vix), apply_manual("move", move)
    macro = {k: apply_manual(k, fetch_macro(k, start_fetch)) for k in MACRO_SRC}
    for k, s in {**px, "fg": fg, "vix": vix, "move": move, **macro}.items():
        DATA_DATES[k] = s.index.max().strftime("%Y-%m-%d")

    cal = px["spx"].index[px["spx"].index >= start_fetch]
    df = pd.DataFrame(index=cal)
    for k, s in px.items():
        df[k] = s.reindex(cal).ffill(limit=3)
    df["fg"] = fg.reindex(cal).ffill(limit=3)
    df["vix"] = vix.reindex(cal).ffill(limit=3)
    df["move"] = move.reindex(cal).ffill(limit=3)
    for k, s in macro.items():
        df[k] = s.reindex(cal).ffill(limit=5)
    # 變化量用各序列自己的觀測日計算 (避免假日補值造成低估)
    df["hy_5d"] = (macro["hy"] - macro["hy"].shift(5)).reindex(cal).ffill(limit=5)
    df["rr_20d"] = (macro["rr"] - macro["rr"].shift(20)).reindex(cal).ffill(limit=5)
    df["ts"] = df["vix"] / df["vix3m"]
    inv = (df["ts"] > 1).astype(int)
    df["inv_days"] = inv.groupby((inv != inv.shift()).cumsum()).cumsum() * inv

    # 相對比較：各指標的恐慌百分位 (越高越恐慌)
    df["fear_fg"] = 100 - rolling_pct(df["fg"])
    df["fear_vix"] = rolling_pct(df["vix"])
    df["fear_move"] = rolling_pct(df["move"])
    # 新增指標的相對位置（過去 252 交易日百分位；僅供參考，不納入綜合恐慌分數）
    df["pct_vxn"] = rolling_pct(df["vxn"])
    df["pct_ts"] = rolling_pct(df["ts"])
    df["pct_hy"] = rolling_pct(df["hy"])
    df["pct_rr"] = rolling_pct(df["rr"])
    df["panic_score"] = sum(df[f"fear_{k}"] * w for k, w in WEIGHTS.items()) / sum(WEIGHTS.values())
    df["equity_panic"] = (df["fear_fg"] + df["fear_vix"]) / 2

    # 往後一個月報酬 & 期間最大回檔
    for k in INDICES:
        p = df[k]
        for h, (n, _) in HORIZONS.items():
            df[f"{k}_fwd{h}"] = p.shift(-n) / p - 1
            fut_min = p[::-1].rolling(n, min_periods=1).min()[::-1].shift(-1)
            df[f"{k}_mdd{h}"] = (fut_min / p - 1).where(df[f"{k}_fwd{h}"].notna())

    hot = (df["fg"] >= RED_FG).astype(int)
    df["fg_hot_days"] = hot.groupby((hot != hot.shift()).cumsum()).cumsum() * hot   # F&G ≥ 75 連續天數
    df["light"] = df.apply(classify, axis=1)
    df["conf"] = confirm_score(df)
    entry_plan(df)
    df = df[df.index >= start_analysis]
    df.index.name = "date"
    return df


def classify(r) -> str:
    sc, fg, vix = r.get("panic_score"), r.get("fg"), r.get("vix")
    if pd.isna(vix):
        return "NA"
    fg_ok = not pd.isna(fg)          # F&G 偶有缺值：VIX ≥ 30 單獨即可成立綠燈
    if vix >= GREEN_VIX_ALONE or (fg_ok and fg <= GREEN_FG and vix >= GREEN_VIX):
        return "GREEN"
    if fg_ok and fg >= RED_FG and (vix < RED_VIX or (pd.notna(r.get("pct_hy")) and r["pct_hy"] <= RED_HY_PCT)):
        strong = (r.get("fg_hot_days") or 0) >= RED_STREAK or fg >= RED_FG_EXTREME
        return "RED2" if strong else "RED"
    if (not pd.isna(sc) and sc >= YELLOW_SCORE) or (fg_ok and fg <= GREEN_FG) or vix >= GREEN_VIX:
        return "YELLOW"
    if not fg_ok:
        return "NA"
    return "NEUTRAL"


def load_events() -> pd.DataFrame:
    if not EVENTS_FILE.exists():
        return pd.DataFrame(columns=["date", "type", "event", "importance"])
    e = pd.read_csv(EVENTS_FILE, comment="#", parse_dates=["date"])
    return e.sort_values("date")


def entry_plan(df: pd.DataFrame) -> None:
    """逐日計算進場狀態（直接寫入 df 欄位）：
    entry_tranche  當日發出第幾批進場訊號（0 = 無）
    entry_phase    WAIT（綠燈後等待轉折）／BUY（進場訊號日）／HOLD（已進場，等待再度轉綠創高）／NONE
    entry_peak     本輪追蹤的分數高點；entry_need = 觸發門檻（高點 − TURN_DROP）
    """
    n = len(df)
    sc, lt = df["panic_score"].values, df["light"].values
    tranche = np.zeros(n, dtype=int)
    phase = np.array(["NONE"] * n, dtype=object)
    peak = np.full(n, np.nan)
    done = np.zeros(n, dtype=int)
    active, last_green, k, pk, need_new_green, green_after = False, -999, 0, np.nan, False, False
    for i in range(n):
        g = lt[i] == "GREEN"
        if g:
            if not active:                                  # 新的一段：第一天先等待
                active, k, pk, need_new_green, green_after = True, 0, sc[i], False, True
            last_green = i
            if need_new_green:                              # 已進場後再度轉綠 → 重新追蹤高點
                need_new_green, green_after, pk = False, True, sc[i]
        if active and i - last_green > EPISODE_TAIL:
            active = False
        if not active:
            continue
        done[i] = k
        if not np.isnan(sc[i]):
            pk = sc[i] if np.isnan(pk) else max(pk, sc[i])
        peak[i] = pk
        if green_after and k < MAX_TRANCHES and not np.isnan(sc[i]) and sc[i] <= pk - TURN_DROP:
            k += 1
            tranche[i] = k
            done[i] = k
            phase[i] = "BUY"
            need_new_green, green_after = True, False
        elif green_after and k < MAX_TRANCHES:
            phase[i] = "WAIT"
        else:
            phase[i] = "HOLD" if k else "WAIT"
    df["entry_tranche"] = tranche
    df["entry_phase"] = phase
    df["entry_peak"] = peak
    df["entry_need"] = peak - TURN_DROP
    df["entry_done"] = done          # 本段已發出的進場批數


def event_note(date: pd.Timestamp, cal: pd.DatetimeIndex | None = None) -> str | None:
    """若進場訊號日之後的下一個交易日有高重要度事件，回傳延後建議。"""
    ev = load_events()
    if ev.empty:
        return None
    nxt = date + pd.offsets.BDay(1)
    hit = ev[(ev["date"] > date) & (ev["date"] <= nxt + pd.Timedelta(days=3)) & (ev["importance"] == "高")]
    hit = hit[hit["date"] <= nxt] if len(hit) else hit
    if hit.empty:
        return None
    e = hit.iloc[0]
    return f"下一個交易日 {e['date']:%m/%d} 有{e['event']}，建議等公布後於 {e['date']:%m/%d} 收盤前執行"


def upcoming_events(start: pd.Timestamp, days: int = 45) -> list[dict]:
    ev = load_events()
    ev = ev[(ev["date"] > start) & (ev["date"] <= start + pd.Timedelta(days=days))]
    return [{"date": f"{r.date:%Y-%m-%d}", "type": r.type, "event": r.event, "importance": r.importance}
            for r in ev.itertuples()]


def confirm_score(df: pd.DataFrame) -> pd.Series:
    inv = (df["inv_days"] >= INV_DAYS_MIN).astype(int)
    vxn = (df["vxn"] >= VXN_HI).astype(int)
    capit = (df["hy"] >= HY_CAPIT).astype(int)
    widen = ((df["hy"] < HY_CAPIT) & (df["hy_5d"] >= HY_WIDEN)).astype(int)
    return inv + vxn + capit - widen


def position_advice(r) -> dict:
    """依燈號 + 確認分數，給出「機會資金」動用比例與加碼／暫停條件 (紀律框架，非投資建議)。"""
    light, conf = r["light"], int(r["conf"]) if pd.notna(r["conf"]) else 0
    notes, adds, pause = [], [], []
    phase, tr = r.get("entry_phase", "NONE"), int(r.get("entry_tranche", 0) or 0)
    done_n = int(r.get("entry_done", 0) or 0)
    nxt = done_n + 1
    pk, need = r.get("entry_peak"), r.get("entry_need")
    key = "conf>=2" if conf >= 2 else ("conf=1" if conf == 1 else "conf<=0")
    if phase == "BUY":
        deploy = DEPLOY[key] if tr == 1 else 1 / 3
        head = (f"第 {tr} 批進場訊號：綜合分數自高點 {pk:.1f} 回落至 {r['panic_score']:.1f} → "
                + (f"動用機會資金 {deploy:.0%}（確認分數 {conf:+d}）" if tr == 1 else "再動用機會資金 1/3"))
        en = event_note(r.name) if hasattr(r, "name") and isinstance(r.name, pd.Timestamp) else None
        if en:
            notes.append(en)
    elif phase == "WAIT" and light == "GREEN":
        deploy = 0.0
        head = (f"綠燈・先等待：綜合分數高點 {pk:.1f}，回落至 ≤ {need:.1f} 才發出第 {nxt} 批進場訊號"
                + (f"（屆時動用機會資金 {DEPLOY[key]:.0%}）" if nxt == 1 else "（屆時再動用機會資金 1/3）")
                + (f"；本段已於前次轉折發出 {done_n} 批" if done_n else ""))
        hi = [e for e in upcoming_events(r.name, 30) if e["importance"] == "高"] if isinstance(r.name, pd.Timestamp) else []
        if hi:
            pre = [f"{(pd.Timestamp(e['date']) - pd.offsets.BDay(1)):%m/%d}（{e['event']}前一日）" for e in hi]
            notes.insert(0, "若轉折訊號出現在 " + "、".join(pre) + "，改在事件公布當日收盤執行")
    elif phase in ("WAIT", "HOLD") and light != "GREEN":
        deploy = 0.0
        head = (f"已發出 {done_n} 批進場訊號，等待再度轉綠並創新高後的轉折（第 {nxt} 批）" if phase == "HOLD"
                else f"綠燈段落追蹤中：分數高點 {pk:.1f}，回落至 ≤ {need:.1f} 為第 1 批")
    elif light == "GREEN":
        deploy = 0.0
        head = "綠燈・先等待分數轉折"
    elif light == "YELLOW":
        deploy, head = 0.0, "黃燈 → 不動用機會資金，等待綠燈"
    elif light in ("RED", "RED2"):
        deploy = 0.0
        if light == "RED2":
            head = "深紅・持續過熱 → 停止加碼，分批獲利了結，現金補回基準水位上緣"
        else:
            head = "紅燈・情緒過熱 → 停止加碼，先減碼高波動部位，現金補回最低水位以上"
        notes.append("過熱不是立即賣出訊號：歷史上紅燈後 1 個月 S&P 仍有約 7 成機率上漲，風險集中在 3 個月內")
        notes.append("費城半導體在紅燈後 3 個月的表現明顯落後大盤，減碼優先從半導體／高 beta／槓桿部位開始")
        adds = ["紅燈：減碼高 beta／槓桿部位約 1/3，停止新增部位",
                "轉深紅（F&G ≥ 75 連續 10 日或 F&G ≥ 85）：再減碼 1/3，現金拉回基準水位上緣",
                "F&G 跌回 60 以下或 VIX 升破 20：過熱解除，停止減碼"]
    else:
        deploy, head = 0.0, "無訊號 → 維持原配置"
    if r.get("ts", 0) >= TS_EXTREME:
        notes.append(f"VIX÷VIX3M {r['ts']:.2f} ≥ {TS_EXTREME}：歷史上 2 週內常再急殺（1 個月內平均最大回檔約 −9%），請至少分 3 批")
    if pd.notna(r.get("hy")) and r["hy"] < HY_CAPIT and pd.notna(r.get("hy_5d")) and r["hy_5d"] >= HY_WIDEN:
        notes.append(f"高收益債利差 {r['hy']:.2f}%，5 日擴大 {r['hy_5d']:+.2f}：信用面惡化中但尚未出清，短線勝率較低")
    if pd.notna(r.get("rr")) and r["rr"] >= RR_HEADWIND:
        notes.append(f"10 年實質利率 {r['rr']:.2f}% ≥ {RR_HEADWIND}%：估值逆風（回測中不是有效的進出場訊號，僅作背景）")
    if light in ("GREEN", "YELLOW") or phase in ("WAIT", "HOLD", "BUY"):
        adds = [f"綠燈後分數自高點回落 ≥ {TURN_DROP:.0f} 分 → 第 1 批；再度轉綠創新高後回落 ≥ {TURN_DROP:.0f} 分 → 下一批（最多 {MAX_TRANCHES} 批）","VIX÷VIX3M 回到 1 以下（期限結構恢復正常＝恐慌見頂）→ 再動用 1/3",
                f"高收益債利差升破 {HY_CAPIT:.0f}% 後停止擴大（信用恐慌出清）→ 再動用 1/3",
                "標普 500 自首批進場價再跌 5% → 再動用 1/3"]
        pause = [f"高收益債利差 ≥ {HY_CRISIS_LVL}% 且 5 日擴大 ≥ {HY_CRISIS_5D} → 暫停加碼（2020 年型態：信用急速惡化）"]
    return {"deploy": deploy, "headline": head, "conf": conf, "notes": notes, "adds": adds, "pause": pause,
            "mode": "exit" if light in ("RED", "RED2") else "entry"}


# ---------------------------------------------------------------- 統計分析
def episodes(idx: pd.DatetimeIndex, gap: int = 10) -> int:
    """計算獨立事件數 (相隔 >gap 交易日視為不同事件)，避免日資料重疊高估樣本。"""
    if len(idx) == 0:
        return 0
    pos = np.searchsorted(CAL, idx)
    return int(1 + (np.diff(pos) > gap).sum())


def stats(sub: pd.DataFrame) -> dict:
    out = {"天數": len(sub), "事件數": episodes(sub.index)}
    for k, name in INDICES.items():
        for h in HORIZONS:
            r = sub[f"{k}_fwd{h}"].dropna()
            e = len(r) > 0
            out[f"{name}_{h}_平均"] = r.mean() if e else np.nan
            out[f"{name}_{h}_中位數"] = r.median() if e else np.nan
            out[f"{name}_{h}_勝率"] = (r > 0).mean() if e else np.nan
            out[f"{name}_{h}_最差10%"] = r.quantile(0.1) if e else np.nan
            out[f"{name}_{h}_期間平均最大回檔"] = sub[f"{k}_mdd{h}"].dropna().mean() if e else np.nan
            out[f"{name}_{h}_樣本"] = len(r)
    return out


def bucket_table(df: pd.DataFrame, col: str, bins, labels) -> pd.DataFrame:
    cat = pd.cut(df[col], bins=bins, labels=labels, right=False, include_lowest=True)
    rows = {"全部 (基準)": stats(df)}
    for lab in labels:
        rows[lab] = stats(df[cat == lab])
    return pd.DataFrame(rows).T


def analyze(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    t = {}
    t["綜合恐慌分數"] = bucket_table(df, "panic_score", [0, 20, 40, 60, 80, 101],
                               ["0-20 極度貪婪", "20-40 貪婪", "40-60 中性", "60-80 恐懼", "80-100 極度恐懼"])
    t["恐懼貪婪指數"] = bucket_table(df, "fg", [0, 25, 45, 55, 75, 101],
                               ["0-25 極度恐懼", "25-45 恐懼", "45-55 中性", "55-75 貪婪", "75-100 極度貪婪"])
    t["VIX"] = bucket_table(df, "vix", [0, 15, 20, 25, 30, 40, 999],
                            ["<15", "15-20", "20-25", "25-30", "30-40", "≥40"])
    t["MOVE"] = bucket_table(df, "move", [0, 80, 100, 120, 150, 999],
                             ["<80", "80-100", "100-120", "120-150", "≥150"])
    # 股市極度恐慌時，債市是否同步恐慌？
    eq = df[(df["fg"] <= 25) & (df["vix"] >= 25)]
    t["股市恐慌×債市"] = pd.DataFrame({
        "全部 (基準)": stats(df),
        "股市極恐 (F&G≤25 且 VIX≥25)": stats(eq),
        "　└ 債市平靜 (MOVE 百分位<70)": stats(eq[eq["fear_move"] < 70]),
        "　└ 債市同步恐慌 (MOVE 百分位≥70)": stats(eq[eq["fear_move"] >= 70]),
    }).T
    A = ((df["fg"] <= GREEN_FG) & (df["vix"] >= GREEN_VIX)) | (df["vix"] >= GREEN_VIX_ALONE)
    hi = df["panic_score"] >= 80
    t["訊號拆解"] = pd.DataFrame({
        "全部 (基準)": stats(df),
        "F&G≤25 且 VIX≥25": stats(df[(df["fg"] <= 25) & (df["vix"] >= 25)]),
        "VIX≥30": stats(df[df["vix"] >= 30]),
        "綜合分數≥80 但未達絕對門檻": stats(df[hi & ~A]),
        "F&G≤25 但 VIX<25": stats(df[(df["fg"] <= 25) & (df["vix"] < 25)]),
    }).T
    if "pct_hy" in df:
        hot = df["fg"] >= RED_FG
        t["過熱訊號拆解"] = pd.DataFrame({
            "全部 (基準)": stats(df),
            "F&G ≥ 75（單看）": stats(df[hot]),
            "F&G ≥ 75 且 VIX < 15": stats(df[hot & (df["vix"] < RED_VIX)]),
            "F&G ≥ 75 且 HY 利差百分位 ≤ 20": stats(df[hot & (df["pct_hy"] <= RED_HY_PCT)]),
            "紅燈（以上任一）": stats(df[df["light"].isin(["RED", "RED2"])]),
            "　└ 深紅（連續 ≥10 日或 F&G ≥ 85）": stats(df[df["light"] == "RED2"]),
            "F&G ≥ 75 連續 ≥ 10 日": stats(df[df["fg_hot_days"] >= RED_STREAK]),
            "F&G ≥ 85": stats(df[df["fg"] >= RED_FG_EXTREME]),
        }).T
        rd = df[df["light"].isin(["RED", "RED2"])]
        if len(rd):
            t["紅燈逐年"] = pd.DataFrame({str(y): stats(g) for y, g in rd.groupby(rd.index.year)}).T
    if "entry_tranche" in df:
        gr = (df["light"] == "GREEN").astype(int)
        prev = gr.shift(1).rolling(EPISODE_TAIL, min_periods=1).max().fillna(0)
        first = df[(gr == 1) & (prev == 0)]
        t["進場時點比較"] = pd.DataFrame({
            "全部 (基準)": stats(df),
            "舊規則：綠燈第一天進場": stats(first),
            f"新規則：第 1 批（分數自高點回落 ≥{TURN_DROP:.0f}）": stats(df[df["entry_tranche"] == 1]),
            "新規則：第 2 批": stats(df[df["entry_tranche"] == 2]),
            "新規則：第 3 批": stats(df[df["entry_tranche"] == 3]),
            "新規則：全部進場訊號日": stats(df[df["entry_tranche"] > 0]),
        }).T
        ev = load_events()
        fomc = set(ev.loc[ev["type"] == "FOMC", "date"]) if len(ev) else set()
        isf = df.index.isin(list(fomc))
        mv = df["spx"].pct_change().abs()
        rows = {}
        for lab, m in (("全部交易日", np.ones(len(df), bool)), ("綜合分數 ≥ 60", (df["panic_score"] >= 60).values),
                       ("綜合分數 ≥ 80", (df["panic_score"] >= 80).values)):
            a, b = mv[m & isf], mv[m & ~isf]
            rows[lab] = {"FOMC 決議日天數": int((m & isf).sum()), "FOMC 決議日平均波動": a.mean(),
                         "一般交易日平均波動": b.mean(), "倍數": a.mean() / b.mean() if b.mean() else np.nan}
        t["重大事件波動"] = pd.DataFrame(rows).T
    t["燈號回測"] = pd.DataFrame({
        "全部 (基準)": stats(df),
        **{f"{LIGHT_ZH[k]}": stats(df[df["light"] == k]) for k in LIGHT_ZH},
    }).T
    yr = df[df["light"] == "GREEN"]
    t["綠燈逐年"] = pd.DataFrame({str(y): stats(g) for y, g in yr.groupby(yr.index.year)}).T

    # ---- 新增指標
    if "vxn" in df:
        t["VXN"] = bucket_table(df, "vxn", [0, 20, 25, 30, 40, 999], ["<20", "20-25", "25-30", "30-40", "≥40"])
        t["VIX÷VIX3M"] = bucket_table(df, "ts", [0, .85, .95, 1, 1.1, 99],
                                      ["<0.85", "0.85-0.95", "0.95-1.00", "1.00-1.10（倒掛）", "≥1.10（深度倒掛）"])
        t["期限結構倒掛天數"] = pd.DataFrame({
            "全部 (基準)": stats(df), "未倒掛": stats(df[df["inv_days"] == 0]),
            "連續倒掛 ≥1 日": stats(df[df["inv_days"] >= 1]), "連續倒掛 ≥3 日": stats(df[df["inv_days"] >= 3]),
            "連續倒掛 ≥5 日": stats(df[df["inv_days"] >= 5])}).T
        t["高收益債利差"] = bucket_table(df, "hy", [0, 3, 3.5, 4, 5, 6, 99],
                                     ["<3%", "3-3.5%", "3.5-4%", "4-5%", "5-6%", "≥6%"])
        t["高收益債利差5日變化"] = bucket_table(df, "hy_5d", [-9, 0, .2, .5, 9],
                                         ["收斂 (<0)", "0 ~ +0.2", "+0.2 ~ +0.5", "≥ +0.5"])
        t["10年實質利率"] = bucket_table(df, "rr", [-9, 0, 1, 1.5, 2, 2.5, 9],
                                     ["<0%", "0-1%", "1-1.5%", "1.5-2%", "2-2.5%", "≥2.5%"])
        G = df["light"] == "GREEN"
        t["綠燈×確認分數"] = pd.DataFrame({
            "綠燈 全部": stats(df[G]),
            "確認分數 ≤ −1": stats(df[G & (df["conf"] <= -1)]), "確認分數 0": stats(df[G & (df["conf"] == 0)]),
            "確認分數 +1": stats(df[G & (df["conf"] == 1)]), "確認分數 ≥ +2": stats(df[G & (df["conf"] >= 2)])}).T
        t["綠燈×新指標"] = pd.DataFrame({
            "綠燈 全部": stats(df[G]),
            "綠燈 且 期限結構倒掛 ≥3 日": stats(df[G & (df["inv_days"] >= 3)]),
            "綠燈 且 未倒掛": stats(df[G & (df["inv_days"] == 0)]),
            "綠燈 且 VIX÷VIX3M ≥ 1.10": stats(df[G & (df["ts"] >= 1.1)]),
            "綠燈 且 VXN ≥ 40": stats(df[G & (df["vxn"] >= 40)]),
            "綠燈 且 HY<5% 並 5日擴大≥0.3": stats(df[G & (df["hy"] < 5) & (df["hy_5d"] >= .3)]),
            "綠燈 且 HY<5% 未明顯擴大": stats(df[G & (df["hy"] < 5) & (df["hy_5d"] < .3)]),
            "綠燈 且 HY ≥ 5%": stats(df[G & (df["hy"] >= 5)]),
            "綠燈 且 實質利率 ≥ 2%": stats(df[G & (df["rr"] >= 2)])}).T
    return t


def recent_signals(df: pd.DataFrame, lookback: int = 126) -> pd.DataFrame:
    """近期訊號追蹤：最近 lookback 個交易日內每一段綠燈 (相隔 >10 交易日為新的一段)，
    列出首次綠燈日、到最新收盤的報酬，以及已滿期的 2 週／1 個月／3 個月報酬。"""
    recent = df.iloc[-lookback:]
    g = recent[recent["light"] == "GREEN"]
    if g.empty:
        return pd.DataFrame()
    pos = np.searchsorted(df.index, g.index)
    grp = np.concatenate([[0], np.cumsum(np.diff(pos) > 10)])
    last = df.iloc[-1]
    rows = []
    for _, e in g.groupby(grp):
        f = e.iloc[0]
        row = {"首次綠燈": e.index[0], "最近綠燈": e.index[-1], "綠燈天數": len(e),
               "F&G": f["fg"], "VIX": f["vix"], "經過交易日": int(len(df) - 1 - df.index.get_loc(e.index[0]))}
        for k in INDICES:
            row[f"{k}_至今"] = last[k] / f[k] - 1
            for h in HORIZONS:
                row[f"{k}_fwd{h}"] = f[f"{k}_fwd{h}"]
        # 分批：該段每個綠燈日各買一份的平均成本 → 至今報酬
        for k in INDICES:
            cost = len(e) / (1 / e[k]).sum()
            row[f"{k}_分批至今"] = last[k] / cost - 1
        rows.append(row)
    return pd.DataFrame(rows).set_index("首次綠燈").sort_index(ascending=False)


def analogs(df: pd.DataFrame, n: int = 12) -> pd.DataFrame:
    """找與今日最相似的歷史日 (以 F&G、VIX、MOVE 百分位距離)，每個事件只取一天。"""
    cols = ["fear_fg", "fear_vix", "fear_move"]
    now = df[cols].iloc[-1]
    hist = df.iloc[:-FWD_DAYS].dropna(subset=cols + ["spx_fwd1m"])
    d = np.sqrt(((hist[cols] - now) ** 2).sum(axis=1)).sort_values()
    picked = []
    for day in d.index:
        if all(abs(np.searchsorted(CAL, day) - np.searchsorted(CAL, p)) > 15 for p in picked):
            picked.append(day)
        if len(picked) >= n:
            break
    out = hist.loc[picked, ["fg", "vix", "move", "panic_score"] +
                   [f"{k}_fwd{h}" for h in HORIZONS for k in INDICES]]
    return out.sort_index(ascending=False)


LIGHT_ZH = {"GREEN": "🟢 綠燈 準備進場（等待轉折）", "YELLOW": "🟡 黃燈 恐慌升溫・等待", "NEUTRAL": "⚪ 灰燈 無訊號",
            "RED": "🔴 紅燈 過熱・退場警示", "RED2": "🔴 深紅 持續過熱・強烈退場"}
LIGHT_COLOR = {"GREEN": "#0ca30c", "YELLOW": "#fab219", "NEUTRAL": "#8a8984", "RED": "#d03b3b", "RED2": "#8e1b1b"}


# ---------------------------------------------------------------- 輸出
def save_db(df: pd.DataFrame, tables: dict, ana: pd.DataFrame):
    DATA.mkdir(exist_ok=True)
    df.to_csv(DATA / "daily.csv", float_format="%.6g")
    with sqlite3.connect(DATA / "fear_signal.db") as con:
        df.reset_index().assign(date=lambda x: x["date"].dt.strftime("%Y-%m-%d")) \
            .to_sql("daily", con, if_exists="replace", index=False)
        for name, t in tables.items():
            t.reset_index(names="分組").to_sql(f"stats_{name}", con, if_exists="replace", index=False)
        ana.reset_index().to_sql("analogs", con, if_exists="replace", index=False)


def build_signal(df: pd.DataFrame, tables: dict) -> dict:
    last = df.iloc[-1]
    prev = df["light"].iloc[-2] if len(df) > 1 else "NA"
    bt = tables["燈號回測"]
    cur_row = bt.loc[LIGHT_ZH[last["light"]]] if last["light"] in LIGHT_ZH else None
    return {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "light": last["light"], "light_zh": LIGHT_ZH.get(last["light"], "資料不足"),
        "changed": bool(last["light"] != prev), "prev_light": prev,
        "fg": round(float(last["fg"]), 1), "vix": round(float(last["vix"]), 2),
        "move": round(float(last["move"]), 2),
        "fear_fg": round(float(last["fear_fg"]), 1), "fear_vix": round(float(last["fear_vix"]), 1),
        "fear_move": round(float(last["fear_move"]), 1),
        "panic_score": round(float(last["panic_score"]), 1),
        "hist_when_same_light": None if cur_row is None else {
            k: (None if pd.isna(v) else round(float(v), 4)) for k, v in cur_row.items()},
        "sources": SOURCES_USED,
        "data_dates": DATA_DATES,
        "stale": [k for k, d in DATA_DATES.items() if d < df.index[-1].strftime("%Y-%m-%d")],
        "macro": {k: (None if pd.isna(last.get(k)) else round(float(last[k]), 4))
                  for k in ("vxn", "vix3m", "ts", "inv_days", "hy", "hy_5d", "rr", "rr_20d", "conf")},
        "advice": position_advice(last),
        "entry": {"phase": last.get("entry_phase"), "tranche": int(last.get("entry_tranche") or 0),
                  "done": int(last.get("entry_done") or 0),
                  "peak": None if pd.isna(last.get("entry_peak")) else round(float(last["entry_peak"]), 1),
                  "need": None if pd.isna(last.get("entry_need")) else round(float(last["entry_need"]), 1),
                  "recent_buys": [f"{d:%Y-%m-%d}（第 {int(t)} 批）" for d, t in
                                  df["entry_tranche"].iloc[-126:][df["entry_tranche"].iloc[-126:] > 0].items()]},
        "events": upcoming_events(df.index[-1]),
    }


def notify(sig: dict):
    """燈號轉為綠燈時推播 (設定環境變數 NTFY_TOPIC，iPhone 安裝 ntfy App 訂閱同名 topic)。"""
    topic = os.getenv("NTFY_TOPIC")
    buy = (sig.get("entry") or {}).get("tranche", 0)
    if not topic or not (buy or (sig["light"] in ("GREEN", "RED", "RED2") and sig["changed"])):
        return
    word = (f"第 {buy} 批進場訊號（分數自高點轉折）" if buy else
            {"GREEN": "轉綠燈（準備進場，等待分數轉折）", "RED": "轉紅燈（過熱・退場警示）",
             "RED2": "轉深紅（持續過熱・強烈退場）"}[sig["light"]])
    msg = (f"{sig['date']} 訊號{word}\n綜合恐慌分數 {sig['panic_score']}\n"
           f"F&G {sig['fg']}｜VIX {sig['vix']}｜MOVE {sig['move']}\n{(sig.get('advice') or {}).get('headline', '')}")
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(),
                      headers={"Title": f"Fear-Signal 第 {buy} 批進場" if buy else f"Fear-Signal {LIGHT_ZH[sig['light']][2:]}", "Priority": "high",
                               "Tags": "moneybag" if buy else ("green_circle" if sig["light"] == "GREEN" else "red_circle")},
                      timeout=15)
        print("已推播通知")
    except Exception as e:  # noqa: BLE001
        print("推播失敗:", e)


# ---------------------------------------------------------------- 網頁
def pct(v, d=1):
    return "—" if v is None or pd.isna(v) else f"{v * 100:+.{d}f}%"


def svg_line(s: pd.Series, h=150, color="#2a78d6", bands=True, fmt="{:.0f}", lo=None, hi=None):
    s = s.dropna()
    w, pl, pr, pt, pb = 640, 34, 8, 8, 20
    lo = s.min() if lo is None else lo
    hi = s.max() if hi is None else hi
    x = lambda i: pl + i / (len(s) - 1) * (w - pl - pr)
    y = lambda v: pt + (1 - (v - lo) / (hi - lo)) * (h - pt - pb)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(s.values))
    g = []
    if bands:
        g.append(f'<rect x="{pl}" y="{y(100):.1f}" width="{w-pl-pr}" height="{y(80)-y(100):.1f}" fill="#0ca30c" opacity=".10"/>')
        g.append(f'<rect x="{pl}" y="{y(20):.1f}" width="{w-pl-pr}" height="{y(0)-y(20):.1f}" fill="#d03b3b" opacity=".08"/>')
    ticks = np.linspace(lo, hi, 5)
    for tv in ticks:
        g.append(f'<line x1="{pl}" x2="{w-pr}" y1="{y(tv):.1f}" y2="{y(tv):.1f}" stroke="var(--grid)" stroke-width="1"/>')
        g.append(f'<text x="{pl-4}" y="{y(tv)+3:.1f}" text-anchor="end" class="ax">{fmt.format(tv)}</text>')
    yrs = sorted({d.year for d in s.index})
    for yr in yrs:
        i = np.searchsorted(s.index, pd.Timestamp(yr, 1, 1))
        if 0 < i < len(s):
            g.append(f'<text x="{x(i):.1f}" y="{h-5}" text-anchor="middle" class="ax">{yr}</text>')
    data = json.dumps([[d.strftime("%Y-%m-%d"), round(float(v), 1)] for d, v in s.items()])
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" data-pts=\'{data}\' data-lo="{lo}" data-hi="{hi}" '
            f'data-pl="{pl}" data-pr="{pr}" data-pt="{pt}" data-pb="{pb}">{"".join(g)}'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'
            f'<line class="xh" y1="{pt}" y2="{h-pb}" stroke="var(--ink2)" stroke-width="1" visibility="hidden"/>'
            f'<circle class="dot" r="4" fill="{color}" stroke="var(--bg)" stroke-width="2" visibility="hidden"/></svg>')


def html_table(t: pd.DataFrame, highlight: str | None = None) -> str:
    head = ("<tr><th rowspan=2>分組</th><th rowspan=2>天數<br><small>事件</small></th>" + "".join(
        f'<th colspan=3 class="grp">{hz}</th>' for _, hz in HORIZONS.values()) + "</tr><tr>" +
        "".join(f"<th>{n}<br><small>平均｜勝率</small></th>" for _ in HORIZONS for n in
                ("S&amp;P", "納指", "半導體")) + "</tr>")
    rows = []
    for lab, r in t.iterrows():
        cls = ' class="hl"' if highlight and lab == highlight else ""
        cells = "".join(
            f'<td><b class="{"pos" if (r[f"{n}_{h}_平均"] or 0) > 0 else "neg"}">{pct(r[f"{n}_{h}_平均"])}</b>'
            f'<br><small>{"—" if pd.isna(r[f"{n}_{h}_勝率"]) else f"{r[f"{n}_{h}_勝率"]*100:.0f}%"}</small></td>'
            for h in HORIZONS for n in INDICES.values())
        rows.append(f"<tr{cls}><td>{lab}</td><td>{int(r['天數'])}<br><small>{"—" if lab.startswith("全部") else int(r['事件數'])}</small></td>{cells}</tr>")
    return f'<div class="tw"><table>{head}{"".join(rows)}</table></div>'


def bucket_of(v, bins, labels):
    for i in range(len(labels)):
        if bins[i] <= v < bins[i + 1]:
            return labels[i]


def tracking_html(trk: pd.DataFrame) -> str:
    if trk is None or trk.empty:
        return '<div class="sub">近半年沒有出現綠燈。</div>'
    head = ("<tr><th>首次綠燈</th><th>綠燈天數</th><th>經過<br><small>交易日</small></th>" +
            "".join(f"<th>{n}<br><small>一次買｜分批</small></th>" for n in ("S&amp;P", "納指", "半導體")) +
            "".join(f"<th>S&amp;P<br><small>{hz}</small></th>" for _, hz in HORIZONS.values()) + "</tr>")
    c = lambda v: "" if pd.isna(v) else ("pos" if v > 0 else "neg")
    rows = "".join(
        f"<tr><td>{d:%Y-%m-%d}<br><small>至 {r['最近綠燈']:%m-%d}</small></td><td>{int(r['綠燈天數'])}</td><td>{r['經過交易日']}</td>"
        + "".join(f"<td><b class='{c(r[f'{k}_至今'])}'>{pct(r[f'{k}_至今'])}</b><br>"
                  f"<small class='{c(r[f'{k}_分批至今'])}'>{pct(r[f'{k}_分批至今'])}</small></td>" for k in INDICES)
        + "".join(f"<td class='{c(r[f'spx_fwd{h}'])}'>{pct(r[f'spx_fwd{h}'])}</td>" for h in HORIZONS) + "</tr>"
        for d, r in trk.iterrows())
    return f'<div class="tw"><table>{head}{rows}</table></div>'


def render_html(df: pd.DataFrame, tables: dict, sig: dict, ana: pd.DataFrame, trk=None) -> str:
    L = sig["light"]
    color = LIGHT_COLOR.get(L, "#8a8984")
    h = sig["hist_when_same_light"] or {}
    tail = df.iloc[-504:]
    sc_lab = bucket_of(sig["panic_score"], [0, 20, 40, 60, 80, 101],
                       list(tables["綜合恐慌分數"].index[1:]))
    fg_lab = bucket_of(sig["fg"], [0, 25, 45, 55, 75, 101], list(tables["恐懼貪婪指數"].index[1:]))
    vix_lab = bucket_of(sig["vix"], [0, 15, 20, 25, 30, 40, 999], list(tables["VIX"].index[1:]))
    mv_lab = bucket_of(sig["move"], [0, 80, 100, 120, 150, 999], list(tables["MOVE"].index[1:]))
    ana_rows = "".join(
        f"<tr><td>{d:%Y-%m-%d}</td><td>{r.fg:.0f}</td><td>{r.vix:.1f}</td><td>{r.move:.0f}</td>"
        + "".join(f"<td class='{'pos' if r[f'{k}_fwd{h}']>0 else 'neg'}'>{pct(r[f'{k}_fwd{h}'])}</td>"
                  for h in HORIZONS for k in INDICES) + "</tr>"
        for d, r in ana.iterrows())
    src = "｜".join(f"{k.upper()}: {v}" for k, v in sig["sources"].items())
    stale_note = lambda k: (f"<small style='color:#c98500'>資料日 {sig['data_dates'][k][5:]}（沿用前值）</small>"
                            if k in sig.get("stale", []) else "")
    hero_tbl = '<table class="mini"><tr><th></th>' + "".join(f"<th>{n}</th>" for n in INDICES.values()) + "</tr>" + "".join(
        f"<tr><td>{hz}</td>" + "".join(
            f"<td><b class='{'pos' if (h.get(f'{n}_{hk}_平均') or 0) > 0 else 'neg'}'>{pct(h.get(f'{n}_{hk}_平均'))}</b>"
            f" <small>{(h.get(f'{n}_{hk}_勝率') or 0)*100:.0f}%</small></td>" for n in INDICES.values()) + "</tr>"
        for hk, (_, hz) in HORIZONS.items()) + "</table>"
    sections = [
        ("綜合恐慌分數 分組", "綜合恐慌分數", sc_lab),
        ("恐懼貪婪指數 分組", "恐懼貪婪指數", fg_lab),
        ("VIX 分組", "VIX", vix_lab),
        ("MOVE 分組", "MOVE", mv_lab),
        ("股市恐慌 × 債市是否同步", "股市恐慌×債市", None),
        ("訊號拆解：為何綠燈看「絕對門檻」", "訊號拆解", None),
        ("燈號回測（訊號當天買進，分別持有 2 週／1 個月／3 個月）", "燈號回測", LIGHT_ZH.get(L)),
        ("綠燈逐年表現（穩定度檢查）", "綠燈逐年", None),
    ]
    if "進場時點比較" in tables:
        sections = [("進場時點：綠燈第一天等待，分數自高點回落 ≥5 才進場", "進場時點比較", None),
                    ("重大事件：FOMC 決議日的波動放大倍數", "重大事件波動", "__none__")] + sections
    if "過熱訊號拆解" in tables:
        sections += [("新增｜過熱紅燈：哪些過熱情境之後表現轉弱", "過熱訊號拆解",
                      {"RED": "紅燈（以上任一）", "RED2": "　└ 深紅（連續 ≥10 日或 F&G ≥ 85）"}.get(L))]
    if "紅燈逐年" in tables:
        sections += [("新增｜紅燈逐年表現", "紅燈逐年", None)]
    if "VXN" in tables:
        m = sig.get("macro", {})
        b = lambda key, v, bins: bucket_of(v, bins, list(tables[key].index[1:])) if v is not None else None
        inv_lab = ("連續倒掛 ≥5 日" if (m.get("inv_days") or 0) >= 5 else "連續倒掛 ≥3 日" if (m.get("inv_days") or 0) >= 3
                   else "連續倒掛 ≥1 日" if (m.get("inv_days") or 0) >= 1 else "未倒掛")
        conf_lab = {True: None}
        c = m.get("conf")
        conf_lab = None if c is None or L != "GREEN" else ("確認分數 ≥ +2" if c >= 2 else "確認分數 +1" if c == 1
                                                          else "確認分數 0" if c == 0 else "確認分數 ≤ −1")
        sections += [
            ("新增｜綠燈 × 確認分數（部位建議的依據）", "綠燈×確認分數", conf_lab),
            ("新增｜綠燈 × 各新指標", "綠燈×新指標", None),
            ("新增｜VXN 分組", "VXN", b("VXN", m.get("vxn"), [0, 20, 25, 30, 40, 999])),
            ("新增｜VIX÷VIX3M 期限結構", "VIX÷VIX3M", b("VIX÷VIX3M", m.get("ts"), [0, .85, .95, 1, 1.1, 99])),
            ("新增｜期限結構連續倒掛天數", "期限結構倒掛天數", inv_lab),
            ("新增｜高收益債利差", "高收益債利差", b("高收益債利差", m.get("hy"), [0, 3, 3.5, 4, 5, 6, 99])),
            ("新增｜高收益債利差 5 日變化", "高收益債利差5日變化", b("高收益債利差5日變化", m.get("hy_5d"), [-9, 0, .2, .5, 9])),
            ("新增｜10 年實質利率", "10年實質利率", b("10年實質利率", m.get("rr"), [-9, 0, 1, 1.5, 2, 2.5, 9])),
        ]
    m = sig.get("macro") or {}
    f = lambda v, fmt: "—" if v is None else fmt.format(v)
    macro_html = "" if not m else (
        '<div class="kpi" style="margin-top:8px">'
        f"<div><small>VXN</small><b>{f(m.get('vxn'), '{:.1f}')}</b>{stale_note('vxn')}</div>"
        f"<div><small>VIX÷VIX3M</small><b>{f(m.get('ts'), '{:.3f}')}</b><small>"
        f"{'倒掛 ' + str(int(m['inv_days'])) + ' 日' if m.get('inv_days') else '正常'}</small></div>"
        f"<div><small>高收益債利差</small><b>{f(m.get('hy'), '{:.2f}%')}</b><small>5 日 {f(m.get('hy_5d'), '{:+.2f}')}</small>{stale_note('hy')}</div>"
        f"<div><small>10年實質利率</small><b>{f(m.get('rr'), '{:.2f}%')}</b>{stale_note('rr')}</div></div>")
    adv = sig.get("advice") or {}
    li = lambda xs: "".join(f"<li>{x}</li>" for x in xs)
    advice_html = "" if not adv else (
        f'<h2>部位建議（紀律框架）</h2><div class="card"><div class="big" style="font-size:17px">{adv["headline"]}</div>'
        + (f'<div class="sub" style="margin-top:6px"><b>注意</b><ul>{li(adv["notes"])}</ul></div>' if adv["notes"] else "")
        + (f'<div class="sub"><b>{"減碼步驟" if adv.get("mode") == "exit" else "加碼條件（各再動用 1/3）"}</b><ul>{li(adv["adds"])}</ul></div>' if adv["adds"] else "")
        + (f'<div class="sub"><b>暫停條件</b><ul>{li(adv["pause"])}</ul></div>' if adv["pause"] else "")
        + '<div class="sub">「機會資金」= 目前現金 − 你設定的最低現金水位。比例依過去 10 年綠燈 × 確認分數回測，僅供紀律參考，非投資建議。</div></div>')
    def any_table(k, hl):
        if k == "重大事件波動":
            t = tables[k]
            return ('<div class="tw"><table><tr><th>情境</th><th>FOMC 決議日</th><th>決議日平均波動</th><th>一般日平均波動</th><th>倍數</th></tr>'
                    + "".join(f"<tr><td>{i}</td><td>{int(r['FOMC 決議日天數'])}</td><td>{r['FOMC 決議日平均波動']*100:.2f}%</td>"
                              f"<td>{r['一般交易日平均波動']*100:.2f}%</td><td><b>{r['倍數']:.2f}×</b></td></tr>" for i, r in t.iterrows())
                    + "</table></div>")
        return html_table(tables[k], hl)
    ev_rows = "".join(f"<tr><td>{e['date'][5:]}</td><td>{'🔴' if e['importance'] == '高' else '⚪'} {e['event']}</td></tr>"
                      for e in sig.get("events", []))
    ev_html = (f'<h2>近期重大事件（台灣時間約晚一天）</h2><div class="card"><div class="sub">🔴 高重要度：進場訊號若落在事件前一個交易日，'
               f'改在事件公布當日收盤執行。</div><div class="tw"><table>{ev_rows}</table></div></div>') if ev_rows else ""
    tbl_html = "".join(f"<h3>{t}</h3>{any_table(k, hl)}" for t, k, hl in sections)
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>恐慌進場燈號 Fear-Signal</title>
<style>
:root{{--bg:#fcfcfb;--card:#fff;--ink:#0b0b0b;--ink2:#52514e;--grid:#e6e5e0;--pos:#1a7f37;--neg:#c0392b}}
@media(prefers-color-scheme:dark){{:root{{--bg:#1a1a19;--card:#242422;--ink:#f0efec;--ink2:#c3c2b7;--grid:#383835;--pos:#3fb950;--neg:#ff7b72}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,"PingFang TC","Noto Sans TC",sans-serif}}
main{{max-width:760px;margin:auto;padding:16px}}h1{{font-size:20px;margin:4px 0}}h2{{font-size:17px;margin:22px 0 6px}}h3{{font-size:15px;margin:18px 0 6px}}
.card{{background:var(--card);border:1px solid var(--grid);border-radius:14px;padding:16px;margin:12px 0}}
.light{{display:flex;align-items:center;gap:16px}}.lamp{{width:72px;height:72px;border-radius:50%;background:{color};box-shadow:0 0 28px {color}88;flex:none}}
.big{{font-size:22px;font-weight:700}}.sub{{color:var(--ink2);font-size:13px}}
.kpi{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}}.kpi div{{background:var(--bg);border-radius:10px;padding:8px;text-align:center}}
.kpi b{{display:block;font-size:18px}}.kpi small{{color:var(--ink2)}}
.tw{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:6px 5px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}th{{color:var(--ink2);font-weight:600}}small{{color:var(--ink2)}}
tr.hl td{{background:{color}22;font-weight:600}}.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}
.chart{{width:100%;height:auto}}.ax{{font-size:10px;fill:var(--ink2)}}.tip{{font-size:12px;color:var(--ink2);min-height:18px}}
ul{{padding-left:18px}}th.grp{{text-align:center;border-bottom:1px solid var(--ink2)}}
.mini{{width:auto;margin-top:4px}}.mini td,.mini th{{padding:3px 8px}}li{{margin:3px 0}}
</style></head><body><main>
<h1>恐慌進場燈號 Fear-Signal</h1><div class="sub">資料日 {sig['date']}（美股收盤）｜更新 {sig['generated_at']}</div>
<div class="card"><div class="light"><div class="lamp" role="img" aria-label="{sig['light_zh']}"></div><div>
<div class="big">{sig['light_zh']}</div>
<div class="sub">綜合恐慌分數 <b>{sig['panic_score']}</b> / 100｜綠燈：F&amp;G≤{GREEN_FG} 且 VIX≥{GREEN_VIX}，或 VIX≥{GREEN_VIX_ALONE}｜紅燈：F&amp;G≥{RED_FG} 且（VIX&lt;{RED_VIX} 或 高收益債利差百分位≤{RED_HY_PCT}）</div>
<div class="sub">歷史同燈號後的平均報酬（勝率）：</div>{hero_tbl}
</div></div>
<div class="kpi"><div><small>恐懼貪婪</small><b>{sig['fg']:.0f}</b><small>恐慌百分位 {sig['fear_fg']:.0f}</small></div>
<div><small>VIX</small><b>{sig['vix']:.1f}</b><small>百分位 {sig['fear_vix']:.0f}</small>{stale_note('vix')}</div>
<div><small>MOVE</small><b>{sig['move']:.0f}</b><small>百分位 {sig['fear_move']:.0f}</small>{stale_note('move')}</div>
<div><small>綜合分數</small><b>{sig['panic_score']:.0f}</b><small>0 貪婪→100 恐慌</small></div></div>
{macro_html}</div>
{advice_html}
{ev_html}

<h2>綜合恐慌分數（近兩年）</h2><div class="card"><div class="tip" id="tip1">輕觸圖表查看數值｜綠帶 ≥80 極度恐慌，紅帶 ≤20 極度貪婪</div>
{svg_line(tail['panic_score'], lo=0, hi=100)}</div>
<h2>S&amp;P 500（近兩年）</h2><div class="card"><div class="tip" id="tip2">輕觸圖表查看數值</div>
{svg_line(tail['spx'], bands=False, color="#2a78d6", fmt="{:,.0f}")}</div>

<h2>歷史統計：訊號後 2 週／1 個月／3 個月報酬</h2>
<div class="sub">過去 {df.index[0]:%Y-%m}～{df.index[-1]:%Y-%m}，每格為「平均報酬｜上漲機率」；反白列 = 今日所在分組。「事件」= 相隔 10 交易日以上的獨立期間數，樣本少時請保守解讀。</div>
{tbl_html}

<h2>近期訊號追蹤（近半年綠燈）</h2><div class="sub">「一次買」= 首次綠燈日買進至今；「分批」= 該段每個綠燈日各買一份的平均成本至今；右側為已滿期的持有報酬（未滿期顯示 —）。</div>
{tracking_html(trk)}
<h2>與今日最相似的歷史時點</h2><div class="sub">依 F&amp;G、VIX、MOVE 三者的相對百分位距離挑選，每個事件取一天</div>
<div class="tw"><table><tr><th>日期</th><th>F&amp;G</th><th>VIX</th><th>MOVE</th>""" + "".join(f"<th>{lab}<br><small>{hz}</small></th>" for _, hz in HORIZONS.values() for lab in ("S&amp;P", "納指", "半導體")) + f"""</tr>{ana_rows}</table></div>

<h2>綜合分數 vs 三大指數（近兩年）</h2>
<div class="card"><img src="score_overview.png" alt="綜合恐慌分數與 S&amp;P 500、納指100、費城半導體指數化走勢" style="width:100%;height:auto"
 onerror="this.parentNode.style.display='none'"></div>
<h2>報告下載</h2>
<div class="card"><ul>
<li><a href="daily_report.pdf">🗓️ 當日報告 PDF</a>（今日燈號、指標、進場計畫、事件、近 10 日紀錄）</li>
<li><a href="report.pdf">📄 10 年資料庫完整報告</a>（燈號回測、各指標分組、總經指標、紅燈）</li>
<li><a href="score_charts.pdf">📈 綜合分數 vs 納指100／S&amp;P 500／費城半導體 線圖</a></li>
<li><a href="ytd_{df.index[-1].year}.pdf">📅 {df.index[-1].year} 年每日燈號回測</a>｜<a href="ytd_{df.index[-1].year - 1}.pdf">{df.index[-1].year - 1} 年</a></li>
</ul></div>
<h2>方法說明</h2><div class="card sub"><ul>
<li>相對比較：各指標換算為「過去 252 個交易日的百分位」，F&amp;G 取反向（越低越恐慌），三者等權平均 = 綜合恐慌分數。只用當時已知資料，無前視偏差。</li>
<li>報酬 = 訊號日收盤買進，持有 10／21／63 個交易日（約 2 週／1 個月／3 個月）後收盤的價格報酬（不含股息）。</li>
<li>回測發現：只看「相對」恐慌（綜合分數≥80）但 VIX、F&amp;G 絕對值未到極端時，後一個月幾乎沒有超額報酬，常是下跌初期；因此綠燈採絕對門檻，綜合分數作為黃燈預警。</li>
<li>燈號門檻為歷史樣本內設定，存在過度配適風險；綠燈代表「歷史上勝率與期望值較佳的區間」，並非保證，建議搭配分批建倉與停損紀律。</li>
<li>資料來源：{src}</li></ul></div>
</main><script>
document.querySelectorAll('svg.chart').forEach((s,k)=>{{const P=JSON.parse(s.dataset.pts),lo=+s.dataset.lo,hi=+s.dataset.hi,
pl=+s.dataset.pl,pr=+s.dataset.pr,pt=+s.dataset.pt,pb=+s.dataset.pb,vb=s.viewBox.baseVal,W=vb.width,H=vb.height,
xh=s.querySelector('.xh'),d=s.querySelector('.dot'),tip=document.getElementById('tip'+(k+1));
function mv(e){{const r=s.getBoundingClientRect(),cx=((e.touches?e.touches[0].clientX:e.clientX)-r.left)/r.width*W;
let i=Math.round((cx-pl)/(W-pl-pr)*(P.length-1));i=Math.max(0,Math.min(P.length-1,i));
const x=pl+i/(P.length-1)*(W-pl-pr),y=pt+(1-(P[i][1]-lo)/(hi-lo))*(H-pt-pb);
xh.setAttribute('x1',x);xh.setAttribute('x2',x);d.setAttribute('cx',x);d.setAttribute('cy',y);
xh.setAttribute('visibility','visible');d.setAttribute('visibility','visible');tip.textContent=P[i][0]+'：'+P[i][1].toLocaleString();}}
s.addEventListener('mousemove',mv);s.addEventListener('touchmove',mv,{{passive:true}});s.addEventListener('touchstart',mv,{{passive:true}});}});
</script></body></html>"""


# ---------------------------------------------------------------- 主程式
CAL = pd.DatetimeIndex([])


def main():
    global CAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--no-pdf", action="store_true", help="不產生任何 PDF 報告")
    ap.add_argument("--reports", default="daily,main,charts,ytd",
                    help="要產出的報告：daily（當日報告）、main（10 年主報告）、charts（綜合分數 vs 三大指數線圖）、ytd（逐日燈號回測），逗號分隔")
    ap.add_argument("--chart-from", type=int, help="線圖／逐日回測起始年（預設去年）")
    a = ap.parse_args()

    df = build_panel(a.years)
    CAL = df.index
    tables = analyze(df)
    ana = analogs(df)
    trk = recent_signals(df)
    sig = build_signal(df, tables)
    sig["recent_green"] = [] if trk.empty else [
        {"first": d.strftime("%Y-%m-%d"), "last": r["最近綠燈"].strftime("%Y-%m-%d"), "days": int(r["綠燈天數"]),
         **{f"{k}_to_now": round(float(r[f"{k}_至今"]), 4) for k in INDICES},
         **{f"{k}_staged_to_now": round(float(r[f"{k}_分批至今"]), 4) for k in INDICES}}
        for d, r in trk.iterrows()]

    save_db(df, tables, ana)
    if not trk.empty:
        with sqlite3.connect(DATA / "fear_signal.db") as con:
            trk.reset_index().astype({"首次綠燈": str, "最近綠燈": str}).to_sql("recent_signals", con, if_exists="replace", index=False)
    DATA.mkdir(exist_ok=True)
    (DATA / "signal.json").write_text(json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render_html(df, tables, sig, ana, trk), encoding="utf-8")
    notify(sig)

    # 每日同步產生 PDF 完整報告 (2 週／1 個月／3 個月)；失敗不影響燈號更新
    if a.no_pdf:
        try:                                     # 不產 PDF 時，仍更新報告數據表
            import report_data
            report_data.save_to_db(archive=False)
        except Exception as e:  # noqa: BLE001
            print("報告數據寫入失敗:", e)
    else:
        # 報告：10 年主報告、綜合分數 vs 三大指數線圖、逐日燈號回測；任一份失敗不影響燈號更新
        try:
            import reports
            reports.make_reports(tuple(k.strip() for k in a.reports.split(",") if k.strip()), a.chart_from, None)
        except Exception as e:  # noqa: BLE001
            print("報告產生失敗（燈號已更新）:", e)

    print(f"\n資料日 {sig['date']}｜{sig['light_zh']}｜綜合恐慌分數 {sig['panic_score']}")
    print(f"F&G {sig['fg']}  VIX {sig['vix']}  MOVE {sig['move']}")
    print("來源:", sig["sources"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
