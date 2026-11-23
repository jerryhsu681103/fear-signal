#!/usr/bin/env python3
"""Fear-Signal 控制台資料更新：從 GitHub 公開資料鏡像重建 history.json（含燈號回測統計）。
用法： python update_history.py OUTPUT_PATH
"""
import io, json, math, sys, urllib.request
from pathlib import Path
import numpy as np
import pandas as pd

RAW = "https://raw.githubusercontent.com"
URL = {
    "fg": f"{RAW}/whit3rabbit/fear-greed-data/main/fear-greed.csv",
    "move": f"{RAW}/kittycapital/move_index/main/data/move_dashboard.json",
    "spx": f"{RAW}/jini-bot/soxx-data/main/ref_gspc_raw.csv",
    "ndx": f"{RAW}/jini-bot/soxx-data/main/qqq_raw.csv",
    "sox": f"{RAW}/jini-bot/soxx-data/main/ref_sox_raw.csv",
    "vix": f"{RAW}/jini-bot/soxx-data/main/ref_vix_raw.csv",
    "vxn": f"{RAW}/achl0902-web/back-testing/main/qld/data/macro/VXNCLS.csv",
    "vix3m": f"{RAW}/achl0902-web/back-testing/main/qld/data/macro/VXVCLS.csv",
    "hy": f"{RAW}/jini-bot/soxx-data/main/fred_hyspread_raw.csv",
    "rr": f"{RAW}/ethanteng/finsight/main/src/datasets/DFII10.csv",
}
MACRO = ("vxn", "vix3m", "hy", "rr")
SRC_NAME = {"fg": "GitHub 鏡像 whit3rabbit/fear-greed-data", "move": "GitHub 鏡像 kittycapital/move_index",
            "spx": "GitHub 鏡像 ^GSPC", "ndx": "GitHub 鏡像（QQQ 價格代理）", "sox": "GitHub 鏡像 ^SOX", "vix": "GitHub 鏡像 ^VIX"}
GREEN_FG, GREEN_VIX, GREEN_VIX_ALONE, YELLOW_SCORE, WIN = 25, 25, 30, 60, 252
HZ = {"2w": 10, "1m": 21, "3m": 63}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r:
        return r.read().decode("utf-8-sig")


def clean(s, positive=True):
    s = pd.to_numeric(s, errors="coerce").dropna()
    s.index = pd.to_datetime(s.index).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s > 0] if positive else s      # 實質利率可能為負


def load_csv_last(url):
    d = pd.read_csv(io.StringIO(get(url)))
    return clean(pd.Series(pd.to_numeric(d.iloc[:, -1], errors="coerce").values, index=pd.to_datetime(d.iloc[:, 0])), positive=False)


def load(seed=None, manual=None):
    out = {}
    fg = pd.read_csv(io.StringIO(get(URL["fg"])), index_col="Date")
    out["fg"] = clean(fg["Fear Greed"])
    mv = json.loads(get(URL["move"]))["series"]["MOVE"]
    out["move"] = clean(pd.Series({pd.to_datetime(t, unit="ms"): v for t, v in mv}))
    for k in ("spx", "ndx", "sox", "vix"):
        out[k] = clean(pd.read_csv(io.StringIO(get(URL[k])), index_col="Date")["Close"])
    sd = pd.read_csv(seed, index_col=0, parse_dates=True) if seed else None
    for k in MACRO:
        try:
            s = load_csv_last(URL[k])
        except Exception as e:  # noqa: BLE001
            print("mirror fail", k, e); s = pd.Series(dtype=float)
        if sd is not None and k in sd:
            s = s.combine_first(clean(sd[k], positive=False))
        out[k] = s
    if manual:
        m = pd.read_csv(manual, comment="#")
        for k, g in m.groupby(m["series"].str.strip().str.lower()):
            if k in out:
                out[k] = clean(pd.Series(g["value"].values, index=pd.to_datetime(g["date"])), positive=k not in ("rr", "hy")).combine_first(out[k])
    return out


def rolling_pct(s):
    return s.rolling(WIN, min_periods=int(WIN * 0.8)).apply(
        lambda a: (a[:-1] < a[-1]).mean() * 100 + (a[:-1] == a[-1]).mean() * 50, raw=True)


def main(path, seed=None, manual=None):
    S = load(seed, manual)
    end = S["spx"].index.max()
    start_fetch = end - pd.DateOffset(years=12)
    start_stats = end - pd.DateOffset(years=10)
    cal = S["spx"].index[S["spx"].index >= start_fetch]
    df = pd.DataFrame({k: s.reindex(cal).ffill(limit=5 if k in MACRO else 3) for k, s in S.items()})
    df["hy_5d"] = (S["hy"] - S["hy"].shift(5)).reindex(cal).ffill(limit=5)
    df["score"] = ((100 - rolling_pct(df["fg"])) + rolling_pct(df["vix"]) + rolling_pct(df["move"])) / 3
    g = ((df.fg <= GREEN_FG) & (df.vix >= GREEN_VIX)) | (df.vix >= GREEN_VIX_ALONE)
    y = (df.score >= YELLOW_SCORE) | (df.fg <= GREEN_FG) | (df.vix >= GREEN_VIX)
    pct_hy = rolling_pct(df["hy"])
    hot = (df.fg >= 75).astype(int)
    hot_days = hot.groupby((hot != hot.shift()).cumsum()).cumsum() * hot
    red = (df.fg >= 75) & ((df.vix < 15) | (pct_hy <= 20))
    red2 = red & ((hot_days >= 10) | (df.fg >= 85))
    df["light"] = np.where(g, "GREEN", np.where(red2, "RED2", np.where(red, "RED", np.where(y, "YELLOW", "NEUTRAL"))))
    for k in ("spx", "ndx", "sox"):
        for h, n in HZ.items():
            df[f"{k}_{h}"] = df[k].shift(-n) / df[k] - 1
    df["ts"] = df.vix / df.vix3m
    inv = (df.ts > 1).astype(int)
    df["inv_days"] = inv.groupby((inv != inv.shift()).cumsum()).cumsum() * inv
    df["conf"] = ((df.inv_days >= 3).astype(int) + (df.vxn >= 40).astype(int) + (df.hy >= 5).astype(int)
                  - ((df.hy < 5) & (df.hy_5d >= .3)).astype(int))
    st = df[df.index >= start_stats].dropna(subset=["score"])
    stats = {}
    G = st.light == "GREEN"
    groups = [("ALL", st)] + [(L, st[st.light == L]) for L in ("GREEN", "YELLOW", "NEUTRAL", "RED", "RED2")] + [
        ("CONF_LE0", st[G & (st.conf <= 0)]), ("CONF_1", st[G & (st.conf == 1)]), ("CONF_GE2", st[G & (st.conf >= 2)])]
    for lab, sub in groups:
        d = {"days": int(len(sub))}
        for k in ("spx", "ndx", "sox"):
            for h in HZ:
                r = sub[f"{k}_{h}"].dropna()
                d[f"{k}_{h}"] = [round(float(r.mean()), 4), round(float((r > 0).mean()), 4)] if len(r) else [None, None]
        stats[lab] = d
    nz = lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 2)
    last_dates = {k: s.index.max().strftime("%Y-%m-%d") for k, s in S.items()}
    rows = [[d.strftime("%Y-%m-%d"), nz(r.fg), nz(r.vix), nz(r.move), nz(r.spx), nz(r.ndx), nz(r.sox),
             nz(r.vxn), nz(r.vix3m), nz(r.hy), nz(r.rr), nz(r.hy_5d)]
            for d, r in df.iterrows()]
    ev_path = Path(seed).parent / "event_calendar.csv" if seed else None
    events = []
    if ev_path and ev_path.exists():
        ev = pd.read_csv(ev_path, comment="#", parse_dates=["date"])
        ev = ev[ev["date"] >= end - pd.Timedelta(days=400)]
        events = [{"date": f"{r.date:%Y-%m-%d}", "type": r.type, "event": r.event, "importance": r.importance} for r in ev.itertuples()]
    out = {"rows": rows, "stats": stats, "events": events, "db_date": end.strftime("%Y-%m-%d"), "stats_from": st.index[0].strftime("%Y-%m-%d"),
           "data_dates": last_dates, "sources": SRC_NAME,
           "updated_at": pd.Timestamp.now(tz="Asia/Taipei").strftime("%Y-%m-%d %H:%M")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    last = df.iloc[-1]
    print(f"OK rows={len(rows)} last={out['db_date']} light={last.light} score={last.score:.1f} data_dates={last_dates}")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0] if a else "history.json", a[1] if len(a) > 1 else None, a[2] if len(a) > 2 else None)
