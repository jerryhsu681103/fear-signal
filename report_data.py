#!/usr/bin/env python3
"""把各份報告的內容寫進資料庫。

主資料庫 data/fear_signal.db（可查詢的表格，每次更新覆寫）：
    report_yearly_summary   逐年燈號回測（10 年主報告第八章）
    report_signal_episodes  10 年所有綠燈／紅燈段落與事後報酬（主報告第五、七章、年度報告第四節）
    report_score_bands      綜合分數五分組 × 指數 × 持有期間的事後報酬（線圖報告下方表格）
    report_daily_actions    每日燈號、確認分數與建議動作（年度報告第五節）
    report_catalog          所有已產出報告的目錄（檔名、種類、期間、頁數、大小、雜湊、產出時間）

報告封存資料庫 data/report_archive.db：
    report_files            PDF 檔案本體（BLOB），每個檔名保留最新一版

用法： python report_data.py   （reports.py 產出報告後也會自動呼叫）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import fear_signal as fs

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "fear_signal.db"
ARCHIVE = ROOT / "data" / "report_archive.db"
IDX = fs.INDICES
HZ = fs.HORIZONS


def load_daily() -> pd.DataFrame:
    with sqlite3.connect(DB) as con:
        return pd.read_sql("select * from daily", con, parse_dates=["date"]).set_index("date")


def action(r) -> str:
    """建議動作（依新進場規則：綠燈先等待，分數自高點回落 ≥5 才進場）。"""
    import fear_signal as _fs
    L, c = r["light"], int(r["conf"]) if pd.notna(r["conf"]) else 0
    tr, ph = int(r.get("entry_tranche", 0) or 0), r.get("entry_phase", "NONE")
    if tr:
        if tr == 1:
            d = _fs.DEPLOY["conf>=2" if c >= 2 else "conf=1" if c == 1 else "conf<=0"]
            return f"第 1 批進場 {d:.0%}"
        return f"第 {tr} 批進場 1/3"
    if L in ("RED", "RED2"):
        return {"RED": "停止加碼・減高 beta 1/3", "RED2": "再減碼 1/3"}[L]
    if ph == "WAIT":
        return "綠燈・等待轉折" if L == "GREEN" else "追蹤中・等待轉折"
    if ph == "HOLD":
        return "已進場・等待再轉綠"
    return {"YELLOW": "等待綠燈", "NEUTRAL": "維持配置"}.get(L, "—")


def daily_actions(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "year": df.index.year, "light": df["light"], "light_zh": df["light"].map(fs.LIGHT_ZH),
        "panic_score": df["panic_score"].round(1), "conf": df["conf"],
        "entry_phase": df.get("entry_phase"), "entry_tranche": df.get("entry_tranche"),
        "entry_peak": df.get("entry_peak"), "entry_need": df.get("entry_need"),
        "action": df.apply(action, axis=1)})
    for k in IDX:
        out[f"{k}_close"] = df[k].round(2)
        for h in HZ:
            out[f"{k}_fwd{h}"] = df[f"{k}_fwd{h}"].round(4)
    out.index.name = "date"
    return out.reset_index().assign(date=lambda x: x["date"].dt.strftime("%Y-%m-%d"))


def yearly_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y in [y for y in sorted(set(df.index.year)) if (df.index.year == y).sum() > 100]:
        d = df[df.index.year == y]
        cnt = d["light"].value_counts()
        g, r = d[d["light"] == "GREEN"], d[d["light"].isin(["RED", "RED2"])]
        n_ep = int(1 + (np.diff(np.searchsorted(d.index, g.index)) > 10).sum()) if len(g) else 0
        g1, g3, r3 = g["spx_fwd1m"].dropna(), g["spx_fwd3m"].dropna(), r["spx_fwd3m"].dropna()
        b1, b3 = d["spx_fwd1m"].mean(), d["spx_fwd3m"].mean()
        row = {"year": y, "trading_days": len(d), "first_date": f"{d.index[0]:%Y-%m-%d}", "last_date": f"{d.index[-1]:%Y-%m-%d}"}
        for k in IDX:
            row[f"{k}_return"] = d[k].iloc[-1] / d[k].iloc[0] - 1
        row["spx_max_drawdown"] = (d["spx"] / d["spx"].cummax() - 1).min()
        for k in ("GREEN", "YELLOW", "NEUTRAL", "RED", "RED2"):
            row[f"days_{k.lower()}"] = int(cnt.get(k, 0))
        row.update({
            "green_episodes": n_ep,
            "green_spx_1m": g1.mean() if len(g1) else None, "green_spx_1m_win": (g1 > 0).mean() if len(g1) else None,
            "green_spx_3m": g3.mean() if len(g3) else None,
            "year_spx_1m": b1, "year_spx_3m": b3,
            "red_spx_3m": r3.mean() if len(r3) else None,
            "score_avg": d["panic_score"].mean(), "score_max": d["panic_score"].max(),
            "green_verdict": None if not len(g1) else ("有效" if g1.mean() > b1 else "偏早"),
            "red_verdict": None if not len(r3) else ("有效" if r3.mean() < b3 else "假警報"),
        })
        rows.append(row)
    return pd.DataFrame(rows)


def signal_episodes(df: pd.DataFrame) -> pd.DataFrame:
    sig = df[df["light"].isin(["GREEN", "RED", "RED2"])]
    kind = np.where(sig["light"] == "GREEN", "GREEN", "RED")
    pos = np.searchsorted(df.index, sig.index)
    brk = np.concatenate([[0], np.cumsum((np.diff(pos) > 10) | (kind[1:] != kind[:-1]))])
    last = df.iloc[-1]
    rows = []
    for _, e in sig.groupby(brk):
        f = e.iloc[0]
        row = {"type": "GREEN" if f.light == "GREEN" else "RED",
               "deep_red": bool((e["light"] == "RED2").any()),
               "first_date": f"{e.index[0]:%Y-%m-%d}", "last_date": f"{e.index[-1]:%Y-%m-%d}", "days": len(e),
               "year": e.index[0].year, "first_conf": int(f.conf) if pd.notna(f.conf) else None,
               "first_action": action(f), "fg": f.fg, "vix": f.vix, "move": f.move, "vxn": f.get("vxn"),
               "hy": f.get("hy"), "panic_score": f.panic_score,
               "fg_extreme": e["fg"].min() if f.light == "GREEN" else e["fg"].max(),
               "vix_extreme": e["vix"].max() if f.light == "GREEN" else e["vix"].min()}
        i1 = df.index.get_loc(e.index[-1])
        win = df.iloc[df.index.get_loc(e.index[0]): i1 + fs.EPISODE_TAIL + 1]
        buys = win[win["entry_tranche"] > 0] if row["type"] == "GREEN" and "entry_tranche" in win else win.iloc[0:0]
        row["rule_first_buy"] = f"{buys.index[0]:%Y-%m-%d}" if len(buys) else None
        row["rule_buys"] = len(buys)
        if len(buys):
            row["rule_spx_fwd1m"] = buys.iloc[0]["spx_fwd1m"]
            row["rule_spx_fwd3m"] = buys.iloc[0]["spx_fwd3m"]
            for k in IDX:
                row[f"{k}_rule_cost_to_latest"] = last[k] / (len(buys) / (1 / buys[k]).sum()) - 1
        for k in IDX:
            for h in HZ:
                row[f"{k}_fwd{h}"] = f[f"{k}_fwd{h}"]
            row[f"{k}_mdd3m"] = f[f"{k}_mdd3m"]
            row[f"{k}_to_latest"] = last[k] / f[k] - 1
            row[f"{k}_staged_to_latest"] = last[k] / (len(e) / (1 / e[k]).sum()) - 1
        rows.append(row)
    out = pd.DataFrame(rows)
    out["latest_date"] = f"{df.index[-1]:%Y-%m-%d}"
    return out


def score_bands(df: pd.DataFrame) -> pd.DataFrame:
    bins, labs = [0, 20, 40, 60, 80, 101], ["0-20", "20-40", "40-60", "60-80", "80-100"]
    rows = []
    y_last = df.index[-1].year
    periods = {"10年": df, f"{y_last-1}-{y_last}": df[df.index.year >= y_last - 1]}
    periods.update({str(y): df[df.index.year == y] for y in sorted(set(df.index.year)) if (df.index.year == y).sum() > 100})
    for per, d in periods.items():
        cat = pd.cut(d["panic_score"], bins=bins, labels=labs, right=False)
        for lab in labs + ["全部"]:
            sub = d if lab == "全部" else d[cat == lab]
            for k, name in IDX.items():
                for h in HZ:
                    x = sub[f"{k}_fwd{h}"].dropna()
                    rows.append({"period": per, "score_band": lab, "index_code": k, "index_name": name, "horizon": h,
                                 "days": len(sub), "samples": len(x), "avg_return": x.mean() if len(x) else None,
                                 "win_rate": (x > 0).mean() if len(x) else None,
                                 "corr_score_vs_return": d["panic_score"].corr(d[f"{k}_fwd{h}"]) if lab == "全部" else None})
    return pd.DataFrame(rows)


def catalog_and_archive(files: list[Path]) -> pd.DataFrame:
    from pypdf import PdfReader
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    ARCHIVE.parent.mkdir(exist_ok=True)
    with sqlite3.connect(ARCHIVE) as arc:
        arc.execute("drop table if exists report_files")     # 只保留目前這一批報告的最新版本
        arc.execute("""create table if not exists report_files (
            name text primary key, kind text, period text, generated_at text, sha256 text, bytes integer,
            pages integer, pdf blob)""")
        for f in files:
            if not f.exists():
                continue
            b = f.read_bytes()
            name = f.name
            kind = ("yearly" if "年度燈號回測報告" in name else "ytd" if name.startswith("ytd_") else
                    "charts" if name.startswith("score_vs_index") else "main" if name.startswith("fear_signal_10y") else "other")
            period = "".join(ch for ch in name if ch.isdigit() or ch == "_").strip("_").replace("_", "-") or "10年"
            pages = len(PdfReader(f).pages)
            sha = hashlib.sha256(b).hexdigest()
            arc.execute("insert or replace into report_files values (?,?,?,?,?,?,?,?)",
                        (name, kind, period, now, sha, len(b), pages, b))
            rows.append({"name": name, "kind": kind, "period": period, "path": str(f.relative_to(ROOT)),
                         "pages": pages, "bytes": len(b), "sha256": sha, "generated_at": now})
    return pd.DataFrame(rows)


def report_files() -> list[Path]:
    """要登錄／封存的報告：主報告、線圖、近兩年逐日回測、各年度獨立報告（較早年度的逐日回測已包含在年度報告內）。"""
    rep = ROOT / "reports"
    y_now = pd.Timestamp.today().year
    keep = [f for f in sorted(rep.glob("*.pdf"))
            if not f.name.startswith("ytd_signal_") or int(f.stem[-4:]) >= y_now - 1]
    return keep + sorted((rep / "yearly").glob("*.pdf"))


def save_to_db(files: list[Path] | None = None, archive: bool = True) -> dict:
    """archive=False 時只更新報告數據表（不動目錄與 PDF 封存），供不產生 PDF 的快速更新使用。"""
    df = load_daily()
    tables = {"report_yearly_summary": yearly_summary(df), "report_signal_episodes": signal_episodes(df),
              "report_score_bands": score_bands(df), "report_daily_actions": daily_actions(df)}
    if archive:
        tables["report_catalog"] = catalog_and_archive(files if files is not None else report_files())
    with sqlite3.connect(DB) as con:
        for name, t in tables.items():
            t.to_sql(name, con, if_exists="replace", index=False)
    return {k: len(v) for k, v in tables.items()}


if __name__ == "__main__":
    for k, n in save_to_db().items():
        print(f"{k:24s} {n:6d} 筆")
    print("PDF 封存：", ARCHIVE)
