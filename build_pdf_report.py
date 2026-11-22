#!/usr/bin/env python3
"""把 data/fear_signal.db 彙整成 PDF 報告 (HTML → Chrome headless 列印)。
用法： python build_pdf_report.py  → reports/fear_signal_10y_report.pdf
"""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import fear_signal as fs

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports"
IDX = fs.INDICES  # spx/ndx/sox -> 中文名
HZ = fs.HORIZONS  # 2w/1m/3m


def p(v, d=1, sign=True):
    if v is None or pd.isna(v):
        return "—"
    return f"{v*100:+.{d}f}%" if sign else f"{v*100:.{d}f}%"


def cls(v):
    return "" if v is None or pd.isna(v) else ("pos" if v > 0 else "neg")


def full_table(t: pd.DataFrame, highlight=None, h="1m") -> str:
    h1 = f'<tr><th rowspan="2" class="l hz">持有 {HZ[h][1]}</th><th rowspan="2">天數</th><th rowspan="2">事件</th>' + "".join(
        f'<th colspan="5" class="grp">{n}</th>' for n in IDX.values()) + "</tr>"
    h2 = "<tr>" + "".join("<th>平均</th><th>中位數</th><th>勝率</th><th>最差10%</th><th>期間回檔</th>"
                          for _ in IDX) + "</tr>"
    rows = []
    for lab, r in t.iterrows():
        c = ' class="hl"' if highlight and lab == highlight else ""
        ev = "—" if str(lab).startswith("全部") else int(r["事件數"])
        cells = ""
        for n in IDX.values():
            cells += (f'<td class="{cls(r[f"{n}_{h}_平均"])} b">{p(r[f"{n}_{h}_平均"])}</td>'
                      f'<td class="{cls(r[f"{n}_{h}_中位數"])}">{p(r[f"{n}_{h}_中位數"])}</td>'
                      f'<td>{p(r[f"{n}_{h}_勝率"],0,False)}</td>'
                      f'<td class="{cls(r[f"{n}_{h}_最差10%"])}">{p(r[f"{n}_{h}_最差10%"])}</td>'
                      f'<td class="neg">{p(r[f"{n}_{h}_期間平均最大回檔"])}</td>')
        rows.append(f'<tr{c}><td class="l">{lab}</td><td>{int(r["天數"])}</td><td>{ev}</td>{cells}</tr>')
    return f'<table class="full"><thead>{h1}{h2}</thead>{"".join(rows)}</table>'


def chart(s, title, lo=None, hi=None, fmt="{:.0f}", bands=False, color="#2a78d6", ref=None, ref_label="綠燈門檻"):
    s = s.dropna()
    w, h, pl, pr, pt, pb = 1000, 165, 46, 10, 10, 22
    lo = s.min() if lo is None else lo
    hi = s.max() if hi is None else hi
    X = lambda i: pl + i / (len(s) - 1) * (w - pl - pr)
    Y = lambda v: pt + (1 - (v - lo) / (hi - lo)) * (h - pt - pb)
    g = []
    if bands:
        g.append(f'<rect x="{pl}" y="{Y(100):.1f}" width="{w-pl-pr}" height="{Y(80)-Y(100):.1f}" fill="#0ca30c" opacity=".10"/>')
        g.append(f'<rect x="{pl}" y="{Y(20):.1f}" width="{w-pl-pr}" height="{Y(0)-Y(20):.1f}" fill="#d03b3b" opacity=".07"/>')
    for tv in np.linspace(lo, hi, 5):
        g.append(f'<line x1="{pl}" x2="{w-pr}" y1="{Y(tv):.1f}" y2="{Y(tv):.1f}" stroke="#e6e5e0"/>')
        g.append(f'<text x="{pl-5}" y="{Y(tv)+3:.1f}" text-anchor="end" class="ax">{fmt.format(tv)}</text>')
    if ref is not None:
        g.append(f'<line x1="{pl}" x2="{w-pr}" y1="{Y(ref):.1f}" y2="{Y(ref):.1f}" stroke="#d03b3b" stroke-dasharray="4 3"/>')
        g.append(f'<text x="{pl+6}" y="{Y(ref)-4:.1f}" class="ax" fill="#d03b3b">{ref_label} {ref}</text>')
    for yr in sorted({d.year for d in s.index}):
        i = np.searchsorted(s.index, pd.Timestamp(yr, 1, 1))
        if 0 < i < len(s):
            g.append(f'<line x1="{X(i):.1f}" x2="{X(i):.1f}" y1="{h-pb}" y2="{h-pb+4}" stroke="#8a8984"/>')
            g.append(f'<text x="{X(i):.1f}" y="{h-5}" text-anchor="middle" class="ax">{yr}</text>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(s.values))
    return (f'<div class="ch"><div class="ct">{title}</div><svg viewBox="0 0 {w} {h}">{"".join(g)}'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linejoin="round"/></svg></div>')


def green_strip(df):
    """綠燈出現期間標示在 S&P 500 走勢上 (以綠色底帶)。"""
    s = df["spx"]
    w, h, pl, pr, pt, pb = 1000, 175, 46, 10, 10, 22
    lo, hi = s.min() * 0.95, s.max() * 1.02
    X = lambda i: pl + i / (len(s) - 1) * (w - pl - pr)
    Y = lambda v: pt + (1 - (v - lo) / (hi - lo)) * (h - pt - pb)
    g = []
    for i in np.where(np.isin(df["light"].values, ["RED", "RED2"]))[0]:
        g.append(f'<rect x="{X(i)-0.6:.1f}" y="{pt}" width="1.6" height="{h-pt-pb}" fill="#d03b3b" opacity=".35"/>')
    gi = np.where(df["light"].values == "GREEN")[0]
    for i in gi:
        g.append(f'<rect x="{X(i)-0.6:.1f}" y="{pt}" width="1.6" height="{h-pt-pb}" fill="#0ca30c" opacity=".35"/>')
    for tv in np.linspace(lo, hi, 5):
        g.append(f'<line x1="{pl}" x2="{w-pr}" y1="{Y(tv):.1f}" y2="{Y(tv):.1f}" stroke="#e6e5e0"/>')
        g.append(f'<text x="{pl-5}" y="{Y(tv)+3:.1f}" text-anchor="end" class="ax">{tv:,.0f}</text>')
    for yr in sorted({d.year for d in s.index}):
        i = np.searchsorted(s.index, pd.Timestamp(yr, 1, 1))
        if 0 < i < len(s):
            g.append(f'<text x="{X(i):.1f}" y="{h-5}" text-anchor="middle" class="ax">{yr}</text>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(s.values))
    return (f'<div class="ch"><div class="ct">S&amp;P 500 收盤與燈號（綠線 = 綠燈進場訊號；紅線 = 紅燈過熱・退場警示）</div><svg viewBox="0 0 {w} {h}">'
            f'{"".join(g)}<polyline points="{pts}" fill="none" stroke="#2a78d6" stroke-width="1.6"/></svg></div>')


def yearly_section(df):
    """八、逐年燈號回測：每年一列的摘要表 + 10 年燈號日曆。"""
    years = [y for y in sorted(set(df.index.year)) if (df.index.year == y).sum() > 100]
    rows = []
    for y in years:
        d = df[df.index.year == y]
        cnt = d["light"].value_counts()
        ret = {k: d[k].iloc[-1] / d[k].iloc[0] - 1 for k in IDX}
        mdd = (d["spx"] / d["spx"].cummax() - 1).min()
        g, r = d[d["light"] == "GREEN"], d[d["light"].isin(["RED", "RED2"])]
        if len(g):
            pos = np.searchsorted(d.index, g.index)
            n_ep = int(1 + (np.diff(pos) > 10).sum())
        else:
            n_ep = 0
        g1, g3, r3 = g["spx_fwd1m"].dropna(), g["spx_fwd3m"].dropna(), r["spx_fwd3m"].dropna()
        b1, b3 = d["spx_fwd1m"].mean(), d["spx_fwd3m"].mean()
        gv = "—" if not len(g1) else ("✓ 有效" if g1.mean() > b1 else "✗ 偏早")
        rv = "—" if not len(r3) else ("✓ 有效" if r3.mean() < b3 else "✗ 假警報")
        cell = lambda v: f"<td class='{cls(v)}'>{p(v)}</td>" if pd.notna(v) else "<td>—</td>"
        rows.append(
            f"<tr><td class='l b'>{y}</td>" + "".join(cell(ret[k]) for k in IDX) + f"<td class='neg'>{p(mdd)}</td>"
            + "".join(f"<td>{int(cnt.get(k, 0)) or ''}</td>" for k in ("GREEN", "YELLOW", "NEUTRAL", "RED", "RED2"))
            + f"<td>{n_ep or ''}</td>"
            + (f"<td class='{cls(g1.mean())} b'>{p(g1.mean())}<br><small>{(g1 > 0).mean()*100:.0f}%</small></td>" if len(g1) else "<td>—</td>")
            + cell(g3.mean() if len(g3) else np.nan) + cell(b1) + cell(r3.mean() if len(r3) else np.nan) + cell(b3)
            + f"<td>{d['panic_score'].mean():.0f}</td><td>{d['panic_score'].max():.0f}</td>"
            + f"<td class='l'>{gv}</td><td class='l'>{rv}</td></tr>")
    head = ("<tr><th class='l' rowspan='2'>年度</th><th colspan='3' class='grp'>全年漲跌</th><th rowspan='2'>S&amp;P<br>年內最大回檔</th>"
            "<th colspan='5' class='grp'>燈號天數</th><th rowspan='2'>綠燈<br>段數</th>"
            "<th colspan='3' class='grp'>綠燈 vs 全年（S&amp;P）</th><th colspan='2' class='grp'>紅燈 vs 全年（S&amp;P）</th>"
            "<th colspan='2' class='grp'>綜合分數</th><th colspan='2' class='grp'>判讀</th></tr><tr>"
            "<th>S&amp;P</th><th>納指</th><th>半導體</th><th>綠</th><th>黃</th><th>灰</th><th>紅</th><th>深紅</th>"
            "<th>綠燈 1M<br><small>勝率</small></th><th>綠燈 3M</th><th>全年 1M</th><th>紅燈 3M</th><th>全年 3M</th>"
            "<th>平均</th><th>最高</th><th>綠燈</th><th>紅燈</th></tr>")
    table = f"<table class='full'><thead>{head}</thead>{''.join(rows)}</table>"
    # 10 年燈號日曆：每列一年，每格一個交易日
    cw, ch, lw = 3.6, 13, 40
    svg = []
    for i, y in enumerate(years):
        d = df[df.index.year == y]
        yy = i * (ch + 3)
        svg.append(f'<text x="{lw-6}" y="{yy+ch-3}" text-anchor="end" class="ax">{y}</text>')
        for j, (dt, r) in enumerate(d.iterrows()):
            svg.append(f'<rect x="{lw + j*cw:.1f}" y="{yy}" width="{cw-0.4:.1f}" height="{ch}" fill="{fs.LIGHT_COLOR.get(r.light, "#ddd")}"/>')
    for m in range(1, 13):
        x = lw + (m - 1) * 21 * cw
        svg.append(f'<text x="{x:.1f}" y="{len(years)*(ch+3)+10}" class="ax">{m}月</text>')
    W, H = lw + 262 * cw, len(years) * (ch + 3) + 14
    cal = f'<svg viewBox="0 0 {W:.0f} {H}" style="width:100%;height:auto">{"".join(svg)}</svg>'
    return table, cal, years


def red_episodes_table(df):
    g = df[df["light"].isin(["RED", "RED2"])]
    if g.empty:
        return "<p class='n'>過去 10 年沒有紅燈。</p>", 0
    pos = np.searchsorted(df.index, g.index)
    grp = np.concatenate([[0], np.cumsum(np.diff(pos) > 10)])
    rows = []
    for _, e in g.groupby(grp):
        f = e.iloc[0]
        deep = (e["light"] == "RED2").any()
        rows.append(
            f"<tr><td class='l'>{e.index[0]:%Y-%m-%d}</td><td class='l'>{e.index[-1]:%Y-%m-%d}</td><td>{len(e)}</td>"
            f"<td>{'深紅' if deep else '紅'}</td><td>{e['fg'].max():.0f}</td><td>{e['vix'].min():.1f}</td>"
            + "".join(f"<td class='{cls(f[f'{k}_fwd{h}'])}'>{p(f[f'{k}_fwd{h}'])}</td>" for h in ("1m", "3m") for k in IDX)
            + f"<td class='neg'>{p(f['spx_mdd3m'])}</td></tr>")
    head = ("<tr><th class='l' rowspan='2'>首次紅燈</th><th class='l' rowspan='2'>最後紅燈</th><th rowspan='2'>天數</th>"
            "<th rowspan='2'>強度</th><th rowspan='2'>F&amp;G 最高</th><th rowspan='2'>VIX 最低</th>"
            "<th colspan='3' class='grp'>首日起 1 個月</th><th colspan='3' class='grp'>首日起 3 個月</th>"
            "<th rowspan='2'>S&amp;P 3 個月內<br>最大回檔</th></tr><tr>"
            + "".join(f"<th>{n}</th>" for n in ("S&amp;P", "納指", "半導體")) * 2 + "</tr>")
    return f"<table class='full'><thead>{head}</thead>{''.join(rows)}</table>", len(rows)


def episodes_table(df):
    g = df[df["light"] == "GREEN"]
    pos = np.searchsorted(df.index, g.index)
    grp = np.concatenate([[0], np.cumsum(np.diff(pos) > 10)])
    rows = []
    for _, e in g.groupby(grp):
        f = e.iloc[0]
        rows.append(
            f"<tr><td class='l'>{e.index[0]:%Y-%m-%d}</td><td class='l'>{e.index[-1]:%Y-%m-%d}</td><td>{len(e)}</td>"
            f"<td>{e['fg'].min():.0f}</td><td>{e['vix'].max():.1f}</td><td>{e['move'].max():.0f}</td>"
            + "".join(f"<td class='{cls(f[f'{k}_fwd{h}'])}{' b' if h == '1m' else ''}'>{p(f[f'{k}_fwd{h}'])}</td>"
                      for h in HZ for k in IDX)
            + "</tr>")
    head = ("<tr><th class='l' rowspan='2'>首次綠燈</th><th class='l' rowspan='2'>最後綠燈</th><th rowspan='2'>綠燈天數</th>"
            "<th rowspan='2'>F&amp;G 最低</th><th rowspan='2'>VIX 最高</th><th rowspan='2'>MOVE 最高</th>"
            + "".join(f"<th colspan='3' class='grp'>首日買進・持有 {hz}</th>" for _, hz in HZ.values()) + "</tr>"
            "<tr>" + "".join(f"<th>{n}</th>" for n in ("S&amp;P", "納指", "半導體")) * 3 + "</tr>")
    return f"<table class='full'>{head}{''.join(rows)}</table>", len(rows)


def nf(v, f):
    return "—" if v is None or pd.isna(v) else f.format(v)


def monthly_table(df):
    agg = {"fg": "last", "vix": "last", "move": "last", "panic_score": "last", "spx": "last", "ndx": "last", "sox": "last"}
    has_macro = "vxn" in df
    if has_macro:
        agg.update({"vxn": "last", "hy": "last", "rr": "last"})
    m = df.resample("ME").agg(agg)
    if has_macro:
        m["ts_max"] = df["ts"].resample("ME").max()
        m["inv_n"] = (df["ts"] > 1).resample("ME").sum()
        m["hy_max"] = df["hy"].resample("ME").max()
    m["fg_min"] = df["fg"].resample("ME").min()
    m["vix_max"] = df["vix"].resample("ME").max()
    m["green"] = (df["light"] == "GREEN").resample("ME").sum()
    m["yellow"] = (df["light"] == "YELLOW").resample("ME").sum()
    for k in IDX:
        m[k + "_m"] = m[k].pct_change()
    m = m.sort_index(ascending=False)
    rows = []
    for d, r in m.iterrows():
        gcls = " class='hl'" if r["green"] > 0 else ""
        rows.append(
            f"<tr{gcls}><td class='l'>{d:%Y-%m}</td><td>{r.fg:.0f}</td><td>{r.fg_min:.0f}</td><td>{r.vix:.1f}</td>"
            f"<td>{r.vix_max:.1f}</td><td>{r.move:.0f}</td><td>{r.panic_score:.0f}</td>"
            f"<td>{int(r.green) or ''}</td><td>{int(r.yellow) or ''}</td>"
            + ((f"<td>{nf(r.vxn, '{:.1f}')}</td><td>{nf(r.ts_max, '{:.2f}')}</td><td>{int(r.inv_n) or ''}</td>"
                f"<td>{nf(r.hy, '{:.2f}')}</td><td>{nf(r.hy_max, '{:.2f}')}</td><td>{nf(r.rr, '{:.2f}')}</td>") if has_macro else "")
            + "".join(f"<td class='{cls(r[k+'_m'])}'>{p(r[k+'_m'])}</td>" for k in IDX) + "</tr>")
    head = ("<tr><th class='l'>月份</th><th>F&amp;G 月底</th><th>F&amp;G 最低</th><th>VIX 月底</th><th>VIX 最高</th>"
            "<th>MOVE 月底</th><th>綜合分數</th><th>綠燈天</th><th>黃燈天</th>"
            + ("<th>VXN 月底</th><th>VIX÷VIX3M 最高</th><th>倒掛天</th><th>HY 月底</th><th>HY 最高</th><th>實質利率 月底</th>"
               if has_macro else "")
            + "".join(f"<th>{n} 當月</th>" for n in IDX.values()) + "</tr>")
    return f"<table class='full mon'><thead>{head}</thead>{''.join(rows)}</table>"


def main(out_dir: Path | None = None) -> Path | None:
    con = sqlite3.connect(ROOT / "data" / "fear_signal.db")
    df = pd.read_sql("select * from daily", con, parse_dates=["date"]).set_index("date")
    fs.CAL = df.index
    tables = fs.analyze(df)
    ana = pd.read_sql("select * from analogs", con, parse_dates=["date"]).set_index("date")
    trk = fs.recent_signals(df)
    red_html, n_red = red_episodes_table(df)
    y_table, y_cal, yrs = yearly_section(df)
    macro_charts = ""
    if "vxn" in df:
        rng = lambda x, st: (float(np.floor(x.min() / st) * st), float(np.ceil(x.max() / st) * st))
        lo, hi = rng(df["vxn"].dropna(), 10)
        macro_charts = ('<div class="pg"></div>'
            + chart(df["vxn"], "VXN 指數（那斯達克100 波動率；虛線 = 確認門檻 40）", lo, hi, color="#d03b3b", ref=40, ref_label="確認門檻")
            + chart(df["ts"], "VIX÷VIX3M 期限結構（虛線 = 1.0，高於 1 為倒掛）", *rng(df["ts"].dropna(), .1), fmt="{:.2f}",
                    color="#6250d6", ref=1.0, ref_label="倒掛線")
            + '<div class="pg"></div>'
            + chart(df["hy"], "高收益債利差 %（ICE BofA OAS；虛線 = 信用出清門檻 5%）", *rng(df["hy"].dropna(), 1), fmt="{:.1f}",
                    color="#eb6834", ref=5, ref_label="出清門檻")
            + chart(df["rr"], "10 年期實質利率 %（TIPS；虛線 = 估值逆風 2.5%）", *rng(df["rr"].dropna(), 1), fmt="{:.1f}",
                    color="#1baf7a", ref=2.5, ref_label="逆風線"))
    if trk.empty:
        trk_html = "<p class='n'>近半年沒有出現綠燈。</p>"
    else:
        th = ("<tr><th class='l'>首次綠燈</th><th class='l'>最近綠燈</th><th>綠燈天數</th><th>經過交易日</th>"
              + "".join(f"<th>{n}<br>一次買</th><th>{n}<br>分批</th>" for n in ("S&amp;P", "納指", "半導體"))
              + "".join(f"<th>S&amp;P<br>{hz}</th>" for _, hz in HZ.values()) + "</tr>")
        tb = "".join(
            f"<tr><td class='l'>{d:%Y-%m-%d}</td><td class='l'>{r['最近綠燈']:%Y-%m-%d}</td><td>{int(r['綠燈天數'])}</td><td>{r['經過交易日']}</td>"
            + "".join(f"<td class='{cls(r[f'{k}_至今'])} b'>{p(r[f'{k}_至今'])}</td><td class='{cls(r[f'{k}_分批至今'])}'>{p(r[f'{k}_分批至今'])}</td>" for k in IDX)
            + "".join(f"<td class='{cls(r[f'spx_fwd{h}'])}'>{p(r[f'spx_fwd{h}'])}</td>" for h in HZ) + "</tr>"
            for d, r in trk.iterrows())
        trk_html = f"<table class='full'>{th}{tb}</table>"
    sig = json.loads((ROOT / "data" / "signal.json").read_text(encoding="utf-8"))
    L = sig["light"]
    lamp = fs.LIGHT_COLOR.get(L, "#8a8984")
    bt = tables["燈號回測"]
    g = bt.loc[fs.LIGHT_ZH["GREEN"]]
    base = bt.loc["全部 (基準)"]
    ep_html, n_ep = episodes_table(df)
    hl = lambda key, bins: fs.bucket_of(sig[key], bins, list(tables[{"panic_score": "綜合恐慌分數", "fg": "恐懼貪婪指數", "vix": "VIX", "move": "MOVE"}[key]].index[1:]))

    V = lambda r, n, h, m="平均": r[f"{n}_{h}_{m}"]
    w = lambda r, n, h: f"{V(r, n, h, '勝率')*100:.0f}%"
    pp = lambda r, n, h: f"{(V(r, n, h) - V(base, n, h))*100:+.1f} 個百分點"
    yr, sx, fgt = tables["綠燈逐年"], tables["訊號拆解"], tables["恐懼貪婪指數"]
    head = ("<tr><th class='l' rowspan='2'>持有期間</th>" + "".join(f"<th colspan='4' class='grp'>{n}</th>" for n in IDX.values())
            + "</tr><tr>" + "<th>綠燈平均</th><th>綠燈勝率</th><th>基準</th><th>超額</th>" * 3 + "</tr>")
    body = "".join(
        f"<tr><td class='l b'>{hz}（{d} 交易日）</td>" + "".join(
            f"<td class='pos b'>{p(V(g, n, h))}</td><td>{w(g, n, h)}</td><td>{p(V(base, n, h))}</td>"
            f"<td class='{cls(V(g, n, h) - V(base, n, h))}'>{p(V(g, n, h) - V(base, n, h))}</td>" for n in IDX.values()) + "</tr>"
        for h, (d, hz) in HZ.items())
    kpi = f"<table class='full big3'>{head}{body}</table>"
    stl = sig.get("stale", [])
    stale_txt = ("<span style='color:#c98500'>（" + "、".join(f"{k.upper()} 最新資料日 {sig['data_dates'][k]}，沿用前值" for k in stl
                 if k in ("fg", "vix", "move")) + "）</span>") if any(k in ("fg", "vix", "move") for k in stl) else ""
    v_, f_ = sig["vix"], sig["fg"]
    if v_ >= fs.GREEN_VIX_ALONE:
        status_note = f"目前 VIX {v_:.1f} ≥ {fs.GREEN_VIX_ALONE}，屬綠燈中恐慌程度較高的一類。"
    elif f_ <= fs.GREEN_FG and v_ >= fs.GREEN_VIX:
        status_note = f"目前 F&amp;G {f_:.0f} ≤ {fs.GREEN_FG} 且 VIX {v_:.1f} ≥ {fs.GREEN_VIX}，符合綠燈。"
    elif L in ("RED", "RED2"):
        status_note = (f"目前 F&amp;G {f_:.0f} ≥ {fs.RED_FG}，且 VIX 偏低或信用利差處於一年低檔：情緒過熱，"
                       + ("已連續過熱或極度貪婪，屬強烈退場警示。" if L == "RED2" else "屬退場警示（停止加碼、先減碼高波動部位）。"))
    else:
        need = []
        if v_ < fs.GREEN_VIX_ALONE:
            need.append(f"VIX 再升至 {fs.GREEN_VIX_ALONE}（目前 {v_:.1f}）")
        if f_ > fs.GREEN_FG or v_ < fs.GREEN_VIX:
            need.append(f"或 F&amp;G 降至 {fs.GREEN_FG} 以下且 VIX ≥ {fs.GREEN_VIX}")
        status_note = "尚未達綠燈：需 " + "，".join(need) + "。"
    ana_rows = "".join(
        f"<tr><td class='l'>{d:%Y-%m-%d}</td><td>{r.fg:.0f}</td><td>{r.vix:.1f}</td><td>{r.move:.0f}</td><td>{r.panic_score:.0f}</td>"
        + "".join(f"<td class='{cls(r[f'{k}_fwd{h}'])}'>{p(r[f'{k}_fwd{h}'])}</td>" for h in HZ for k in IDX) + "</tr>"
        for d, r in ana.iterrows())

    sec = lambda t, k, h=None, note="": (f"<div class='blk'><h3>{t}</h3>{('<p class=n>'+note+'</p>') if note else ''}"
                                         + "".join(full_table(tables[k], h, hz) for hz in HZ) + "</div>")
    _sig = json.loads((ROOT / "data" / "signal.json").read_text(encoding="utf-8"))
    m, adv = _sig.get("macro") or {}, _sig.get("advice") or {}
    if "VXN" in tables and adv:
        li = lambda xs: "".join(f"<li>{x}</li>" for x in xs)
        fmt = lambda v, f: "—" if v is None else f.format(v)
        macro_sec = (
            '<div class="pg"></div><h2>六、新增總經指標與部位建議</h2>'
            f'<div class="hero" style="display:block"><div class="big" style="font-size:14pt">{adv["headline"]}</div>'
            f'<div>VXN {fmt(m.get("vxn"), "{:.2f}")}｜VIX÷VIX3M {fmt(m.get("ts"), "{:.3f}")}（連續倒掛 {int(m.get("inv_days") or 0)} 日）｜'
            f'高收益債利差 {fmt(m.get("hy"), "{:.2f}%")}（5 日 {fmt(m.get("hy_5d"), "{:+.2f}")}）｜10 年實質利率 {fmt(m.get("rr"), "{:.2f}%")}</div>'
            + (f'<div class="two"><div><b>注意</b><ul>{li(adv["notes"])}</ul></div>'
               f'<div><b>加碼條件（各再動用 1/3）</b><ul>{li(adv["adds"])}</ul><b>暫停條件</b><ul>{li(adv["pause"])}</ul></div></div>')
            + '<div class="sub">確認分數：+1 期限結構連續倒掛 ≥3 日；+1 VXN ≥ 40；+1 高收益債利差 ≥ 5%；−1 利差 < 5% 但 5 日擴大 ≥ 0.3。'
              '動用比例：分數 ≤0 → 20%、+1 → 33%、≥+2 → 50%（機會資金 = 目前現金 − 最低現金水位）。紀律框架，非投資建議。</div></div>'
            + sec('6-1 綠燈 × 確認分數', '綠燈×確認分數')
            + sec('6-2 綠燈 × 各新指標', '綠燈×新指標')
            + '<div class="pg"></div>' + sec('6-3 VXN（那斯達克100 波動率）', 'VXN')
            + sec('6-4 VIX÷VIX3M 期限結構', 'VIX÷VIX3M')
            + '<div class="pg"></div>' + sec('6-5 期限結構連續倒掛天數', '期限結構倒掛天數')
            + sec('6-6 高收益債利差（水準）', '高收益債利差')
            + '<div class="pg"></div>' + sec('6-7 高收益債利差 5 日變化', '高收益債利差5日變化')
            + sec('6-8 10 年實質利率', '10年實質利率', note='≥2.5% 的樣本僅 5 天，無統計意義；實質利率在回測中不是有效的進出場訊號，作為估值背景參考。'))
    else:
        macro_sec = ""
    red_sec = ""
    if "過熱訊號拆解" in tables:
        rt = tables["燈號回測"]
        r1, r2 = rt.loc[fs.LIGHT_ZH["RED"]], rt.loc[fs.LIGHT_ZH["RED2"]]
        bb = rt.loc["全部 (基準)"]
        red_sec = (
            '<div class="pg"></div><h2>七、過熱紅燈（退場警示）</h2>'
            f'<div class="hero" style="display:block"><div class="big" style="font-size:13pt">紅燈：F&amp;G ≥ {fs.RED_FG} 且（VIX &lt; {fs.RED_VIX} 或 高收益債利差一年百分位 ≤ {fs.RED_HY_PCT}）'
            f'｜深紅：再加上 F&amp;G ≥ {fs.RED_FG} 連續 ≥ {fs.RED_STREAK} 日，或 F&amp;G ≥ {fs.RED_FG_EXTREME}</div>'
            '<div class="two"><div><ul>'
            f'<li><b>不是立即賣出訊號：</b>紅燈後 1 個月 S&amp;P 平均 {p(r1["S&P 500_1m_平均"])}、上漲機率 {r1["S&P 500_1m_勝率"]*100:.0f}%，與基準相近，動能通常延續。</li>'
            f'<li><b>風險在 3 個月：</b>深紅後 3 個月 S&amp;P 平均 {p(r2["S&P 500_3m_平均"])}、上漲機率僅 {r2["S&P 500_3m_勝率"]*100:.0f}%，'
            f'期間平均最大回檔 {p(r2["S&P 500_3m_期間平均最大回檔"])}（基準 {p(bb["S&P 500_3m_平均"])}／{bb["S&P 500_3m_勝率"]*100:.0f}%／{p(bb["S&P 500_3m_期間平均最大回檔"])}）。</li>'
            f'<li><b>半導體最弱：</b>紅燈期間費城半導體 3 個月平均 {p(r1["費城半導體_3m_平均"])}、深紅 {p(r2["費城半導體_3m_平均"])}（基準 {p(bb["費城半導體_3m_平均"])}）。</li>'
            '</ul></div><div><ul>'
            '<li><b>紅燈：</b>停止新增部位；高 beta／槓桿部位減碼約 1/3；現金補回最低水位以上。</li>'
            '<li><b>深紅：</b>再減碼 1/3，現金拉回基準水位上緣。</li>'
            '<li><b>解除：</b>F&amp;G 跌回 60 以下或 VIX 升破 20 → 停止減碼，等待下一次綠燈再分批買回。</li>'
            f'<li><b>樣本提醒：</b>紅燈 {n_red} 段、深紅僅 7 段，3 個月轉弱主要來自 2018、2020 年；2017、2023 年紅燈後仍續漲，宜分批而非一次出清。</li>'
            '</ul></div></div></div>'
            + sec('7-1 過熱訊號拆解（各種過熱情境之後的報酬）', '過熱訊號拆解')
            + ('<div class="pg"></div>' + sec('7-2 紅燈逐年表現', '紅燈逐年') if "紅燈逐年" in tables else "")
            + f'<h3>7-3 紅燈事件全紀錄（{n_red} 段）</h3>' + red_html)
    src = "；".join(f"{k.upper()}：{v}" for k, v in sig["sources"].items())
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8"><title>恐慌情緒與後續報酬（2 週／1 個月／3 個月）：10 年資料庫報告</title>
<style>
@page{{size:A4 landscape;margin:12mm 12mm 14mm}}
body{{font-family:"Noto Sans CJK TC","Noto Sans TC","PingFang TC",sans-serif;color:#0b0b0b;font-size:9.5pt;line-height:1.45;margin:0}}
h1{{font-size:20pt;margin:0 0 4px}}h2{{font-size:13.5pt;margin:0 0 6px;padding-bottom:3px;border-bottom:2px solid #0b0b0b}}
h3{{font-size:10.5pt;margin:12px 0 4px}}.n{{color:#52514e;font-size:8.5pt;margin:0 0 4px}}
.sub{{color:#52514e}}.pg{{page-break-before:always}}
.hero{{display:flex;gap:18px;align-items:center;border:1px solid #e6e5e0;border-radius:10px;padding:12px 16px;margin:10px 0}}
.lamp{{width:58px;height:58px;border-radius:50%;background:{lamp};flex:none}}.big{{font-size:16pt;font-weight:700}}
.kp{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:8px 0}}.kp div,.k{{background:#f5f5f2;border-radius:8px;padding:7px 9px}}
.k b,.kp b{{display:block;font-size:14pt}}.k small,.kp small{{color:#52514e;display:block;font-size:8pt}}
.ks{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:8px 0}}
table{{border-collapse:collapse;width:100%;font-size:8pt}}th,td{{padding:3px 4px;border-bottom:1px solid #e6e5e0;text-align:right;white-space:nowrap}}
th{{color:#52514e;font-weight:600;background:#fafaf8}}th.grp{{text-align:center;border-bottom:1px solid #c9c8c2}}.l{{text-align:left}}
tr.hl td{{background:#0ca30c1c}}.b{{font-weight:700}}.pos{{color:#1a7f37}}.neg{{color:#c0392b}}
.mon td,.mon th{{padding:1.6px 4px;font-size:7.4pt}}tr{{page-break-inside:avoid}}thead{{display:table-header-group}}
.ch{{margin:6px 0 4px;page-break-inside:avoid}}.ct{{font-size:9pt;font-weight:600;margin-bottom:2px}}svg{{width:100%;height:auto}}.ax{{font-size:10px;fill:#52514e}}
ul{{margin:4px 0;padding-left:18px}}li{{margin:2px 0}}.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
th.hz{{color:#0b0b0b;font-weight:700}}table.full:not(.mon){{page-break-inside:avoid}}.blk h3{{page-break-after:avoid}}.big3 td,.big3 th{{font-size:9pt;padding:5px 6px}}
.foot{{color:#8a8984;font-size:7.5pt;margin-top:8px}}
</style></head><body>

<h1>恐慌情緒與後續報酬（2 週／1 個月／3 個月）：10 年資料庫報告</h1>
<div class="sub">恐懼貪婪指數 × VIX × MOVE → S&amp;P 500／納指100／費城半導體｜樣本期間 {df.index[0]:%Y-%m-%d} ～ {df.index[-1]:%Y-%m-%d}（{len(df):,} 個交易日）｜報告產出 {pd.Timestamp.now():%Y-%m-%d}</div>

<div class="hero"><div class="lamp"></div><div>
<div class="big">目前訊號：{sig['light_zh']}（資料日 {sig['date']}）</div>
<div>綜合恐慌分數 <b>{sig['panic_score']}</b> / 100｜恐懼貪婪 {sig['fg']:.1f}｜VIX {sig['vix']:.2f}｜MOVE {sig['move']:.2f}{stale_txt}</div>
<div class="sub">綠燈條件：(F&amp;G ≤ 25 且 VIX ≥ 25) 或 VIX ≥ 30。{status_note}</div>
<div style="margin-top:4px"><b>部位建議：{(adv or {}).get("headline", "—")}</b>（詳見第六章）</div></div></div>

<h2>一、核心結論：綠燈後 2 週／1 個月／3 個月</h2>
{kpi}
<div class="sub" style="margin:4px 0 8px">過去 10 年「綠燈日收盤買進」分別持有 10／21／63 個交易日的平均報酬與勝率（共 {int(g['天數'])} 個綠燈日、{n_ep} 段獨立事件）；基準 = 所有交易日；超額 = 綠燈 − 基準。</div>
<div class="two"><div><ul>
<li><b>持有越久、優勢越大：</b>S&amp;P 超額報酬 2 週 {pp(g,'S&P 500','2w')}、1 個月 {pp(g,'S&P 500','1m')}、3 個月 {pp(g,'S&P 500','3m')}；費城半導體 3 個月平均 {p(V(g,'費城半導體','3m'))}。</li>
<li><b>勝率在 1 個月最高：</b>S&amp;P 勝率 2 週 {w(g,'S&P 500','2w')}、1 個月 {w(g,'S&P 500','1m')}、3 個月 {w(g,'S&P 500','3m')}。2 週雜訊較多；1 個月是「勝率 × 報酬」最平衡的持有期。</li>
<li><b>3 個月的風險在熊市：</b>2022 年綠燈後 3 個月 S&amp;P 僅 {p(V(yr.loc['2022'],'S&P 500','3m'))}、納指 {p(V(yr.loc['2022'],'納指100','3m'))}（勝率 {w(yr.loc['2022'],'納指100','3m')}）；2021 年納指亦為 {p(V(yr.loc['2021'],'納指100','3m'))}。遇空頭趨勢，宜在 1 個月左右分批獲利了結。</li>
<li><b>絕對恐慌才有優勢：</b>VIX ≥ 30 時 S&amp;P 1 個月 {p(V(sx.loc['VIX≥30'],'S&P 500','1m'))}（勝率 {w(sx.loc['VIX≥30'],'S&P 500','1m')}）、3 個月 {p(V(sx.loc['VIX≥30'],'S&P 500','3m'))}；綜合分數 ≥80 但未達絕對門檻時，2 週與 1 個月勝率僅約 {w(sx.loc['綜合分數≥80 但未達絕對門檻'],'S&P 500','2w')}～{w(sx.loc['綜合分數≥80 但未達絕對門檻'],'S&P 500','1m')}。</li>
</ul></div><div><ul>
<li><b>慢性恐懼是 3 個月機會：</b>F&amp;G ≤ 25 但 VIX &lt; 25 時，2 週／1 個月幾乎無超額（S&amp;P 勝率 {w(sx.loc['F&G≤25 但 VIX<25'],'S&P 500','2w')}／{w(sx.loc['F&G≤25 但 VIX<25'],'S&P 500','1m')}），但 3 個月勝率達 {w(sx.loc['F&G≤25 但 VIX<25'],'S&P 500','3m')}、半導體 {p(V(sx.loc['F&G≤25 但 VIX<25'],'費城半導體','3m'))}：適合中期分批，不適合短線。</li>
<li><b>極度貪婪在 3 個月轉弱：</b>F&amp;G ≥ 75 後 3 個月 S&amp;P 平均僅 {p(V(fgt.loc['75-100 極度貪婪'],'S&P 500','3m'))}、勝率 {w(fgt.loc['75-100 極度貪婪'],'S&P 500','3m')}（基準 {p(V(base,'S&P 500','3m'))}／{w(base,'S&P 500','3m')}），短期則無明顯差異。</li>
<li><b>半導體彈性最大：</b>綠燈後費城半導體 1 個月 {p(V(g,'費城半導體','1m'))}、3 個月 {p(V(g,'費城半導體','3m'))}，約為 S&amp;P 的 2 倍，但期間回檔也較深（3 個月內平均 {p(V(g,'費城半導體','3m','期間平均最大回檔'))}）。</li>
<li><b>進場紀律：</b>綠燈後 S&amp;P 平均期間回檔：2 週 {p(V(g,'S&P 500','2w','期間平均最大回檔'))}、1 個月 {p(V(g,'S&P 500','1m','期間平均最大回檔'))}、3 個月 {p(V(g,'S&P 500','3m','期間平均最大回檔'))}，建議分 2–3 批建倉。</li>
</ul></div></div>

<div class="pg"></div><h2>二、10 年走勢總覽</h2>
{green_strip(df)}
{chart(df['panic_score'], '綜合恐慌分數（0 = 極度貪婪，100 = 極度恐慌；綠帶 ≥80，紅帶 ≤20）', 0, 100, bands=True, color='#6250d6')}
{chart(df['fg'], 'CNN 恐懼貪婪指數（虛線 = 綠燈門檻 25）', 0, 100, color='#eb6834', ref=25)}

<div class="pg"></div>
{chart(df['vix'], 'VIX 指數（虛線 = 單獨綠燈門檻 30）', 0, float(np.ceil(df['vix'].max()/10)*10), color='#d03b3b', ref=30)}
{chart(df['move'], 'MOVE 指數（美債選擇權波動率）', float(np.floor(df['move'].min()/10)*10), float(np.ceil(df['move'].max()/10)*10), color='#1baf7a')}
{macro_charts}
<h3>方法說明</h3><ul>
<li><b>相對比較：</b>三個指標各自換算成「過去 252 個交易日的百分位」，恐懼貪婪指數取反向（越低越恐慌），三者等權平均為綜合恐慌分數。計算時只使用當日以前的資料，沒有前視偏差。</li>
<li><b>報酬定義：</b>訊號日收盤買進，持有 10／21／63 個交易日（約 2 週／1 個月／3 個月）後收盤的價格報酬（不含股息）。「期間回檔」= 持有期間內相對買進價的最大跌幅平均。「最差10%」= 報酬分布的第 10 百分位。</li>
<li><b>事件數：</b>日資料前後高度重疊，因此另計「相隔 10 個交易日以上」的獨立事件數；事件數少於 10 的分組請保守解讀。</li>
<li><b>限制：</b>燈號門檻以同一段 10 年資料設定，有過度配適風險；歷史統計不保證未來報酬。</li></ul>

<div class="pg"></div><h2>三、各指標分組統計（2 週／1 個月／3 個月報酬）</h2>
<p class="n">每個分組依序列出持有 2 週、1 個月、3 個月三張表。反白列 = 目前所在分組。勝率 = 持有期滿報酬為正的比例。近 3 個月的訊號日尚無 3 個月報酬，故 3 個月樣本略少。</p>
{sec('3-1 綜合恐慌分數（相對比較）', '綜合恐慌分數', hl('panic_score',[0,20,40,60,80,101]))}
{sec('3-2 CNN 恐懼貪婪指數', '恐懼貪婪指數', hl('fg',[0,25,45,55,75,101]))}
<div class="pg"></div>
{sec('3-3 VIX 指數', 'VIX', hl('vix',[0,15,20,25,30,40,999]))}
{sec('3-4 MOVE 指數', 'MOVE', hl('move',[0,80,100,120,150,999]))}

<div class="pg"></div><h2>四、組合訊號與燈號回測</h2>
{sec('4-1 訊號拆解：為何綠燈採絕對門檻', '訊號拆解')}
{sec('4-2 股市極度恐慌時，債市是否同步恐慌', '股市恐慌×債市', note='「債市平靜」樣本僅個位數，僅供參考。')}
{sec('4-3 燈號回測', '燈號回測', fs.LIGHT_ZH.get(L))}
<div class="pg"></div>
{sec('4-4 綠燈逐年表現', '綠燈逐年')}
<h3>4-5 近期訊號追蹤（近半年綠燈）</h3><p class="n">「一次買」= 首次綠燈日買進至今；「分批」= 該段每個綠燈日各買一份的平均成本至今。</p>
{trk_html}
<h3>4-6 與目前最相似的歷史時點</h3><p class="n">依恐懼貪婪、VIX、MOVE 三者的相對百分位距離挑選，每個事件取一天。</p>
<table class="full"><tr><th class="l">日期</th><th>F&amp;G</th><th>VIX</th><th>MOVE</th><th>綜合分數</th>{''.join(f'<th>{n}<br>{hz}</th>' for _, hz in HZ.values() for n in ('S&amp;P', '納指', '半導體'))}</tr>{ana_rows}</table>

<div class="pg"></div><h2>五、綠燈事件全紀錄（{n_ep} 段）</h2>
<p class="n">相隔 10 個交易日以上視為不同事件。「首日買進」= 該段第一個綠燈日收盤買進；最近一段尚未滿一個月者顯示「—」。</p>
{ep_html}

{macro_sec}
{red_sec}
<div class="pg"></div><h2>八、逐年燈號回測（{yrs[0]}–{yrs[-1]}）</h2>
<p class="n">每年一列。「綠燈 1M」= 當年綠燈日買進持有 1 個月的 S&amp;P 平均報酬（括號為上漲機率），與「全年 1M」（當年所有交易日）比較；
「紅燈 3M」與「全年 3M」比較。判讀：綠燈 1M 高於全年 = 有效，否則為偏早；紅燈 3M 低於全年 = 有效，否則為假警報。
各年度逐日明細見 reports/yearly/&lt;年&gt;年度燈號回測報告.pdf。</p>
{y_table}
<div style="page-break-inside:avoid"><h3>10 年燈號日曆（每格一個交易日；綠 = 綠燈、黃 = 黃燈、灰 = 灰燈、紅／深紅 = 過熱）</h3>
{y_cal}</div>
<div class="pg"></div><h2>附錄：月度資料庫（{df.index[0]:%Y-%m} ～ {df.index[-1]:%Y-%m}）</h2>
<p class="n">反白 = 當月曾亮綠燈。指數欄為當月價格漲跌幅；HY = 高收益債利差（%）、倒掛天 = 當月 VIX÷VIX3M 大於 1 的天數。完整每日資料（{len(df):,} 筆）見 data/fear_signal.db 與 daily.csv。</p>
{monthly_table(df)}
<div class="foot">資料來源：{src}。納指100 若標示 QQQ 代理，代表以 QQQ 收盤價計算報酬。本報告僅供研究參考，不構成投資建議。</div>
</body></html>"""
    out = out_dir or OUT
    out.mkdir(parents=True, exist_ok=True)
    hp = out / "report.html"
    hp.write_text(html, encoding="utf-8")
    pdf = out / "fear_signal_10y_report.pdf"
    chrome = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)
    if not chrome:
        print("找不到 Chrome，已輸出 HTML：", hp)
        return None
    subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", hp.as_uri()], check=True, capture_output=True)
    shutil.copy(pdf, ROOT / "docs" / "report.pdf")      # 供 GitHub Pages 下載
    print("PDF：", pdf)
    return pdf


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
