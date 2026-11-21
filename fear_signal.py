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
FWD_DAYS = 21          # 往後一個月 = 21 個交易日
PCT_WINDOW = 252       # 相對比較：過去一年 (252 交易日) 的百分位
WEIGHTS = {"fg": 1 / 3, "vix": 1 / 3, "move": 1 / 3}   # 綜合恐慌分數權重

# 燈號規則（依過去 10 年回測結果設定，取整數門檻以降低過度配適）
#   綠燈：股市「絕對」恐慌到位 → (F&G ≤ 25 且 VIX ≥ 25) 或 VIX ≥ 30
#   黃燈：恐慌升溫但未到位 → 綜合恐慌分數 ≥ 60、或 F&G ≤ 25、或 VIX ≥ 25
#   灰燈：無明顯優勢，維持原有配置
GREEN_FG, GREEN_VIX, GREEN_VIX_ALONE = 25, 25, 30
YELLOW_SCORE = 60

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


# ---------------------------------------------------------------- 抓資料
def _get(url: str, timeout: int = 30) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _clean(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s > 0]


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

    cal = px["spx"].index[px["spx"].index >= start_fetch]
    df = pd.DataFrame(index=cal)
    for k, s in px.items():
        df[k] = s.reindex(cal).ffill(limit=3)
    df["fg"] = fg.reindex(cal).ffill(limit=3)
    df["vix"] = vix.reindex(cal).ffill(limit=3)
    df["move"] = move.reindex(cal).ffill(limit=3)

    # 相對比較：各指標的恐慌百分位 (越高越恐慌)
    df["fear_fg"] = 100 - rolling_pct(df["fg"])
    df["fear_vix"] = rolling_pct(df["vix"])
    df["fear_move"] = rolling_pct(df["move"])
    df["panic_score"] = sum(df[f"fear_{k}"] * w for k, w in WEIGHTS.items()) / sum(WEIGHTS.values())
    df["equity_panic"] = (df["fear_fg"] + df["fear_vix"]) / 2

    # 往後一個月報酬 & 期間最大回檔
    for k in INDICES:
        p = df[k]
        df[f"{k}_fwd1m"] = p.shift(-FWD_DAYS) / p - 1
        fut_min = p[::-1].rolling(FWD_DAYS, min_periods=1).min()[::-1].shift(-1)
        df[f"{k}_fwd_mdd"] = (fut_min / p - 1).where(df[f"{k}_fwd1m"].notna())

    df["light"] = df.apply(classify, axis=1)
    df = df[df.index >= start_analysis]
    df.index.name = "date"
    return df


def classify(r) -> str:
    sc, fg, vix = r.get("panic_score"), r.get("fg"), r.get("vix")
    if pd.isna(fg) or pd.isna(vix):
        return "NA"
    if (fg <= GREEN_FG and vix >= GREEN_VIX) or vix >= GREEN_VIX_ALONE:
        return "GREEN"
    if (not pd.isna(sc) and sc >= YELLOW_SCORE) or fg <= GREEN_FG or vix >= GREEN_VIX:
        return "YELLOW"
    return "NEUTRAL"


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
        r = sub[f"{k}_fwd1m"].dropna()
        out[f"{name}_平均"] = r.mean() if len(r) else np.nan
        out[f"{name}_中位數"] = r.median() if len(r) else np.nan
        out[f"{name}_勝率"] = (r > 0).mean() if len(r) else np.nan
        out[f"{name}_最差10%"] = r.quantile(0.1) if len(r) else np.nan
        out[f"{name}_期間平均最大回檔"] = sub[f"{k}_fwd_mdd"].dropna().mean() if len(r) else np.nan
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
    t["燈號回測"] = pd.DataFrame({
        "全部 (基準)": stats(df),
        **{f"{LIGHT_ZH[k]}": stats(df[df["light"] == k]) for k in LIGHT_ZH},
    }).T
    yr = df[df["light"] == "GREEN"]
    t["綠燈逐年"] = pd.DataFrame({str(y): stats(g) for y, g in yr.groupby(yr.index.year)}).T
    return t


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
    out = hist.loc[picked, ["fg", "vix", "move", "panic_score", "spx_fwd1m", "ndx_fwd1m", "sox_fwd1m"]]
    return out.sort_index(ascending=False)


LIGHT_ZH = {"GREEN": "🟢 綠燈 可進場", "YELLOW": "🟡 黃燈 恐慌升溫・等待", "NEUTRAL": "⚪ 灰燈 無訊號"}


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
    }


def notify(sig: dict):
    """燈號轉為綠燈時推播 (設定環境變數 NTFY_TOPIC，iPhone 安裝 ntfy App 訂閱同名 topic)。"""
    topic = os.getenv("NTFY_TOPIC")
    if not topic or not (sig["light"] == "GREEN" and sig["changed"]):
        return
    msg = (f"{sig['date']} 恐慌訊號轉綠燈\n綜合恐慌分數 {sig['panic_score']}\n"
           f"F&G {sig['fg']}｜VIX {sig['vix']}｜MOVE {sig['move']}")
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(),
                      headers={"Title": "Fear-Signal 綠燈", "Priority": "high", "Tags": "green_circle"},
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
    head = "<tr><th>分組</th><th>天數<br><small>事件</small></th>" + "".join(
        f"<th>{n}<br><small>平均｜勝率</small></th>" for n in INDICES.values()) + "</tr>"
    rows = []
    for lab, r in t.iterrows():
        cls = ' class="hl"' if highlight and lab == highlight else ""
        cells = "".join(
            f'<td><b class="{"pos" if (r[f"{n}_平均"] or 0) > 0 else "neg"}">{pct(r[f"{n}_平均"])}</b>'
            f'<br><small>{"—" if pd.isna(r[f"{n}_勝率"]) else f"{r[f"{n}_勝率"]*100:.0f}%"}</small></td>'
            for n in INDICES.values())
        rows.append(f"<tr{cls}><td>{lab}</td><td>{int(r['天數'])}<br><small>{"—" if lab.startswith("全部") else int(r['事件數'])}</small></td>{cells}</tr>")
    return f'<div class="tw"><table>{head}{"".join(rows)}</table></div>'


def bucket_of(v, bins, labels):
    for i in range(len(labels)):
        if bins[i] <= v < bins[i + 1]:
            return labels[i]


def render_html(df: pd.DataFrame, tables: dict, sig: dict, ana: pd.DataFrame) -> str:
    L = sig["light"]
    color = {"GREEN": "#0ca30c", "YELLOW": "#fab219", "NEUTRAL": "#8a8984"}.get(L, "#8a8984")
    h = sig["hist_when_same_light"] or {}
    tail = df.iloc[-504:]
    sc_lab = bucket_of(sig["panic_score"], [0, 20, 40, 60, 80, 101],
                       list(tables["綜合恐慌分數"].index[1:]))
    fg_lab = bucket_of(sig["fg"], [0, 25, 45, 55, 75, 101], list(tables["恐懼貪婪指數"].index[1:]))
    vix_lab = bucket_of(sig["vix"], [0, 15, 20, 25, 30, 40, 999], list(tables["VIX"].index[1:]))
    mv_lab = bucket_of(sig["move"], [0, 80, 100, 120, 150, 999], list(tables["MOVE"].index[1:]))
    ana_rows = "".join(
        f"<tr><td>{d:%Y-%m-%d}</td><td>{r.fg:.0f}</td><td>{r.vix:.1f}</td><td>{r.move:.0f}</td>"
        f"<td class='{'pos' if r.spx_fwd1m>0 else 'neg'}'>{pct(r.spx_fwd1m)}</td>"
        f"<td class='{'pos' if r.ndx_fwd1m>0 else 'neg'}'>{pct(r.ndx_fwd1m)}</td>"
        f"<td class='{'pos' if r.sox_fwd1m>0 else 'neg'}'>{pct(r.sox_fwd1m)}</td></tr>"
        for d, r in ana.iterrows())
    src = "｜".join(f"{k.upper()}: {v}" for k, v in sig["sources"].items())
    sections = [
        ("綜合恐慌分數 分組", "綜合恐慌分數", sc_lab),
        ("恐懼貪婪指數 分組", "恐懼貪婪指數", fg_lab),
        ("VIX 分組", "VIX", vix_lab),
        ("MOVE 分組", "MOVE", mv_lab),
        ("股市恐慌 × 債市是否同步", "股市恐慌×債市", None),
        ("訊號拆解：為何綠燈看「絕對門檻」", "訊號拆解", None),
        ("燈號回測（訊號當天買進持有一個月）", "燈號回測", LIGHT_ZH.get(L)),
        ("綠燈逐年表現（穩定度檢查）", "綠燈逐年", None),
    ]
    tbl_html = "".join(f"<h3>{t}</h3>{html_table(tables[k], hl)}" for t, k, hl in sections)
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
ul{{padding-left:18px}}li{{margin:3px 0}}
</style></head><body><main>
<h1>恐慌進場燈號 Fear-Signal</h1><div class="sub">資料日 {sig['date']}（美股收盤）｜更新 {sig['generated_at']}</div>
<div class="card"><div class="light"><div class="lamp" role="img" aria-label="{sig['light_zh']}"></div><div>
<div class="big">{sig['light_zh']}</div>
<div class="sub">綜合恐慌分數 <b>{sig['panic_score']}</b> / 100｜綠燈條件：F&amp;G≤{GREEN_FG} 且 VIX≥{GREEN_VIX}，或 VIX≥{GREEN_VIX_ALONE}</div>
<div class="sub">歷史同燈號後一個月：S&amp;P {pct(h.get('S&P 500_平均'))}（勝率 {(h.get('S&P 500_勝率') or 0)*100:.0f}%）｜納指 {pct(h.get('納指100_平均'))}｜半導體 {pct(h.get('費城半導體_平均'))}</div>
</div></div>
<div class="kpi"><div><small>恐懼貪婪</small><b>{sig['fg']:.0f}</b><small>恐慌百分位 {sig['fear_fg']:.0f}</small></div>
<div><small>VIX</small><b>{sig['vix']:.1f}</b><small>百分位 {sig['fear_vix']:.0f}</small></div>
<div><small>MOVE</small><b>{sig['move']:.0f}</b><small>百分位 {sig['fear_move']:.0f}</small></div>
<div><small>綜合分數</small><b>{sig['panic_score']:.0f}</b><small>0 貪婪→100 恐慌</small></div></div></div>

<h2>綜合恐慌分數（近兩年）</h2><div class="card"><div class="tip" id="tip1">輕觸圖表查看數值｜綠帶 ≥80 極度恐慌，紅帶 ≤20 極度貪婪</div>
{svg_line(tail['panic_score'], lo=0, hi=100)}</div>
<h2>S&amp;P 500（近兩年）</h2><div class="card"><div class="tip" id="tip2">輕觸圖表查看數值</div>
{svg_line(tail['spx'], bands=False, color="#2a78d6", fmt="{:,.0f}")}</div>

<h2>歷史統計：往後一個月報酬</h2>
<div class="sub">過去 {df.index[0]:%Y-%m}～{df.index[-1]:%Y-%m}，每格為「平均報酬｜上漲機率」；反白列 = 今日所在分組。「事件」= 相隔 10 交易日以上的獨立期間數，樣本少時請保守解讀。</div>
{tbl_html}

<h2>與今日最相似的歷史時點</h2><div class="sub">依 F&amp;G、VIX、MOVE 三者的相對百分位距離挑選，每個事件取一天</div>
<div class="tw"><table><tr><th>日期</th><th>F&amp;G</th><th>VIX</th><th>MOVE</th><th>S&amp;P 1M</th><th>納指 1M</th><th>半導體 1M</th></tr>{ana_rows}</table></div>

<h2>方法說明</h2><div class="card sub"><ul>
<li>相對比較：各指標換算為「過去 252 個交易日的百分位」，F&amp;G 取反向（越低越恐慌），三者等權平均 = 綜合恐慌分數。只用當時已知資料，無前視偏差。</li>
<li>報酬 = 訊號日收盤買進、21 個交易日後收盤的價格報酬（不含股息）。</li>
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
    a = ap.parse_args()

    df = build_panel(a.years)
    CAL = df.index
    tables = analyze(df)
    ana = analogs(df)
    sig = build_signal(df, tables)

    save_db(df, tables, ana)
    DATA.mkdir(exist_ok=True)
    (DATA / "signal.json").write_text(json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render_html(df, tables, sig, ana), encoding="utf-8")
    notify(sig)

    print(f"\n資料日 {sig['date']}｜{sig['light_zh']}｜綜合恐慌分數 {sig['panic_score']}")
    print(f"F&G {sig['fg']}  VIX {sig['vix']}  MOVE {sig['move']}")
    print("來源:", sig["sources"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
