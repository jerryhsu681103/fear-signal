#!/usr/bin/env python3
"""當日報告：把最新一個交易日的燈號、指標、進場計畫、風險提示、事件與近 10 日紀錄整理成 2–3 頁 PDF。
用法： python build_daily_report.py            → reports/daily/daily_report_<資料日>.pdf，並複製到 docs/daily_report.pdf
      python build_daily_report.py 2026-11-09 → 指定歷史日期（用資料庫數據重算當日畫面）
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import fear_signal as fs
from build_pdf_report import p, cls
from build_ytd_report import action

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "daily"
IDX = fs.INDICES
HZ = fs.HORIZONS
LAMP = {"GREEN": "#0ca30c", "YELLOW": "#e0a100", "NEUTRAL": "#8a8984", "RED": "#d03b3b", "RED2": "#8e1b1b"}
KEEP = 30                                            # reports/daily 保留最近 30 個交易日
PHASE = {"WAIT": "等待轉折", "BUY": "今日進場", "HOLD": "已進場・等待再轉綠", "NONE": "無進行中的訊號段"}


def f(v, fmt="{:.1f}"):
    return "—" if v is None or pd.isna(v) else fmt.format(v)


def mini_chart(s, title, color, light=None, fmt="{:.0f}", ref=None, ref_label=""):
    """近 6 個月走勢；底部色條 = 每日燈號；x 軸標月份。"""
    s = s.dropna()
    w, h, pl, pr, pt, pb = 1000, 190, 52, 12, 10, 34
    lo, hi = float(s.min()), float(s.max())
    pad = (hi - lo) * .06 or 1
    lo, hi = lo - pad, hi + pad
    if ref is not None:
        lo, hi = min(lo, ref - pad), max(hi, ref + pad)
    X = lambda i: pl + i / max(len(s) - 1, 1) * (w - pl - pr)
    Y = lambda v: pt + (1 - (v - lo) / (hi - lo)) * (h - pt - pb)
    g = []
    for tv in np.linspace(lo, hi, 5):
        g.append(f'<line x1="{pl}" x2="{w-pr}" y1="{Y(tv):.1f}" y2="{Y(tv):.1f}" stroke="#e6e5e0"/>'
                 f'<text x="{pl-6}" y="{Y(tv)+4:.1f}" text-anchor="end" class="ax">{fmt.format(tv)}</text>')
    if ref is not None:
        g.append(f'<line x1="{pl}" x2="{w-pr}" y1="{Y(ref):.1f}" y2="{Y(ref):.1f}" stroke="#d03b3b" stroke-dasharray="4 3"/>'
                 f'<text x="{pl+6}" y="{Y(ref)-4:.1f}" class="ax" fill="#d03b3b">{ref_label}</text>')
    last_m = None
    for i, d in enumerate(s.index):
        if d.month != last_m:
            last_m = d.month
            if i:
                g.append(f'<line x1="{X(i):.1f}" x2="{X(i):.1f}" y1="{pt}" y2="{h-pb}" stroke="#f0efea"/>')
            if i or d.day <= 12:                   # 起點落在月中就不標，避免與下個月份標籤重疊
                g.append(f'<text x="{X(i)+3:.1f}" y="{h-4}" class="ax">{d:%Y/%m}</text>')
    if light is not None:
        bw = (w - pl - pr) / max(len(s) - 1, 1)
        for i, d in enumerate(s.index):
            L = light.get(d)
            if L in LAMP and L != "NEUTRAL":
                g.append(f'<rect x="{X(i)-bw/2:.1f}" y="{h-pb+4}" width="{bw+.3:.1f}" height="8" fill="{LAMP[L]}"/>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(s.values))
    g.append(f'<circle cx="{X(len(s)-1):.1f}" cy="{Y(s.iloc[-1]):.1f}" r="4.5" fill="{color}" stroke="#fff" stroke-width="2"/>')
    return (f'<div class="ch"><div class="ct">{title}</div><svg viewBox="0 0 {w} {h}">{"".join(g)}'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/></svg></div>')


def chg(s, d, n):
    i = s.index.get_loc(d)
    return s.iloc[i] / s.iloc[i - n] - 1 if i >= n else np.nan


def main(date: str | None = None) -> Path:
    con = sqlite3.connect(ROOT / "data" / "fear_signal.db")
    df = pd.read_sql("select * from daily", con, parse_dates=["date"]).set_index("date")
    fs.CAL = df.index
    d = df.index[-1] if not date else df.index[df.index <= pd.Timestamp(date)][-1]
    full = df.loc[:d]
    r, prev = full.iloc[-1], full.iloc[-2]
    sig = json.loads((ROOT / "data" / "signal.json").read_text(encoding="utf-8"))
    latest = d == df.index[-1]
    if latest:
        adv, ent, evs = sig.get("advice") or {}, sig.get("entry") or {}, sig.get("events") or []
    else:                                            # 歷史日期：用資料庫重算
        adv = fs.position_advice(r)
        ent = {"phase": r.get("entry_phase"), "tranche": int(r.get("entry_tranche") or 0), "done": int(r.get("entry_done") or 0),
               "peak": r.get("entry_peak"), "need": r.get("entry_need"), "recent_buys": []}
        evs = fs.upcoming_events(d, 45) if hasattr(fs, "upcoming_events") else []
    L = r["light"]
    lamp = LAMP.get(L, "#8a8984")
    conf = int(r["conf"]) if pd.notna(r["conf"]) else 0

    # ── KPI：當日值、較前日、一年百分位
    def kpi(name, key, fmt, pct=None, extra="", hi_bad=True):
        v, pv = r.get(key), prev.get(key)
        dv = (v - pv) if pd.notna(v) and pd.notna(pv) else np.nan
        dcls = "" if pd.isna(dv) or dv == 0 else ("neg" if (dv > 0) == hi_bad else "pos")
        pc = f"<small>一年百分位 {pct:.0f}</small>" if pct is not None and pd.notna(pct) else ""
        return (f"<div><small>{name}</small><b>{f(v, fmt)}</b>"
                f"<small class='{dcls}'>較前日 {f(dv, '{:+.2f}') if pd.notna(dv) else '—'}</small>{pc}{extra}</div>")
    kp = "".join([
        kpi("綜合恐慌分數", "panic_score", "{:.1f}"),
        kpi("恐懼貪婪 F&amp;G", "fg", "{:.0f}", 100 - r["fear_fg"] if pd.notna(r["fear_fg"]) else None, hi_bad=False),
        kpi("VIX", "vix", "{:.2f}", r["fear_vix"]),
        kpi("MOVE", "move", "{:.1f}", r["fear_move"]),
        kpi("VXN", "vxn", "{:.2f}", r.get("pct_vxn")),
        kpi("VIX÷VIX3M", "ts", "{:.3f}", r.get("pct_ts"), f"<small>連續倒掛 {int(r['inv_days'] or 0)} 日</small>"),
        kpi("高收益債利差 %", "hy", "{:.2f}", r.get("pct_hy"), f"<small>5 日 {f(r.get('hy_5d'), '{:+.2f}')}</small>"),
        kpi("10 年實質利率 %", "rr", "{:.2f}", r.get("pct_rr"), f"<small>20 日 {f(r.get('rr_20d'), '{:+.2f}')}</small>"),
    ])

    # ── 三大指數快照
    idx_rows = ""
    for k, n in IDX.items():
        s = full[k].dropna()
        hi52 = s.iloc[-252:].max()
        ma200 = s.iloc[-200:].mean()
        c1, c5, c21 = chg(s, s.index[-1], 1), chg(s, s.index[-1], 5), chg(s, s.index[-1], 21)
        idx_rows += (f"<tr><td class='l b'>{n}</td><td>{s.iloc[-1]:,.2f}</td>"
                     + "".join(f"<td class='{cls(x)}'>{p(x)}</td>" for x in (c1, c5, c21))
                     + f"<td class='{cls(s.iloc[-1]/hi52-1)}'>{p(s.iloc[-1]/hi52-1)}</td>"
                       f"<td class='{cls(s.iloc[-1]/ma200-1)}'>{p(s.iloc[-1]/ma200-1)}</td></tr>")
    idx_tbl = ("<table><tr><th class='l'>指數</th><th>收盤</th><th>1 日</th><th>5 日</th><th>1 個月</th>"
               f"<th>距 52 週高點</th><th>距 200 日均線</th></tr>{idx_rows}</table>")

    # ── 進場計畫
    ph = ent.get("phase") or "NONE"
    peak, need = ent.get("peak"), ent.get("need")
    gap = (r["panic_score"] - need) if need is not None and pd.notna(need) else None
    buys = ent.get("recent_buys") or []
    plan = (f"<table class='kv'><tr><td>進場階段</td><td class='b'>{PHASE.get(ph, ph)}</td></tr>"
            f"<tr><td>本段已發出批數</td><td>{ent.get('done', 0)} / {fs.MAX_TRANCHES}</td></tr>"
            f"<tr><td>本段分數高點</td><td>{f(peak)}</td></tr>"
            f"<tr><td>下一批觸發（分數 ≤）</td><td class='b'>{f(need)}</td></tr>"
            f"<tr><td>距觸發還差</td><td>{'—' if gap is None else f'{gap:.1f} 分' if gap > 0 else '已達觸發'}</td></tr>"
            f"<tr><td>確認分數 → 首批動用</td><td>{conf:+d} → {fs.DEPLOY['conf>=2' if conf >= 2 else 'conf=1' if conf == 1 else 'conf<=0']:.0%} 機會資金</td></tr>"
            f"<tr><td>近期進場日</td><td>{'、'.join(buys) if buys else '—'}</td></tr></table>")
    li = lambda xs: "".join(f"<li>{x}</li>" for x in xs or [])

    # ── 歷史參考：同燈號、同確認分數
    def hist_row(name, m):
        sub = df[m]
        cells = ""
        for h in HZ:
            x = sub[f"spx_fwd{h}"].dropna()
            cells += (f"<td class='{cls(x.mean())}'>{p(x.mean())}</td><td>{(x > 0).mean()*100:.0f}%</td>" if len(x)
                      else "<td>—</td><td>—</td>")
        x = sub["ndx_fwd1m"].dropna(); y = sub["sox_fwd1m"].dropna()
        return (f"<tr><td class='l'>{name}</td><td>{len(sub)}</td>{cells}"
                f"<td class='{cls(x.mean())}'>{p(x.mean())}</td><td class='{cls(y.mean())}'>{p(y.mean())}</td></tr>")
    cmask = df["conf"].fillna(0).clip(-1, 2) == min(max(conf, -1), 2)
    hist = ("<table><tr><th class='l'>情境（過去 10 年）</th><th>天數</th>"
            + "".join(f"<th>S&amp;P {hz}<br>平均</th><th>上漲率</th>" for _, hz in HZ.values())
            + "<th>納指 1 個月</th><th>半導體 1 個月</th></tr>"
            + hist_row("全部交易日（基準）", df["light"].notna())
            + hist_row(f"同燈號：{fs.LIGHT_ZH.get(L, L)}", df["light"] == L)
            + hist_row(f"同燈號 × 確認分數 {conf:+d}", (df["light"] == L) & cmask)
            + hist_row("進場批次日（新規則）", df["entry_tranche"].fillna(0) > 0)
            + "</table>")
    try:
        ana = pd.read_sql("select * from analogs", con, parse_dates=["date"]).set_index("date").head(6) if latest else pd.DataFrame()
    except Exception:  # noqa: BLE001
        ana = pd.DataFrame()
    ana_tbl = "" if ana.empty else (
        "<h3>最相似的歷史日期（F&amp;G、VIX、MOVE 最接近）</h3><table><tr><th class='l'>日期</th><th>F&amp;G</th><th>VIX</th><th>MOVE</th><th>分數</th>"
        + "".join(f"<th>S&amp;P {hz}</th>" for _, hz in HZ.values()) + "<th>納指 1 個月</th><th>半導體 1 個月</th></tr>"
        + "".join(f"<tr><td class='l'>{a:%Y-%m-%d}</td><td>{f(x.fg, '{:.0f}')}</td><td>{x.vix:.1f}</td><td>{x.move:.0f}</td><td>{x.panic_score:.0f}</td>"
                  + "".join(f"<td class='{cls(x[f'spx_fwd{h}'])}'>{p(x[f'spx_fwd{h}'])}</td>" for h in HZ)
                  + f"<td class='{cls(x['ndx_fwd1m'])}'>{p(x['ndx_fwd1m'])}</td><td class='{cls(x['sox_fwd1m'])}'>{p(x['sox_fwd1m'])}</td></tr>"
                  for a, x in ana.iterrows()) + "</table>")

    # ── 事件
    ev_rows = "".join(
        f"<tr{' class=hi' if e.get('importance') == '高' else ''}><td class='l'>{e['date']}（{'一二三四五六日'[pd.Timestamp(e['date']).weekday()]}）</td>"
        f"<td class='l'>{e['event']}</td><td>{e.get('importance', '')}</td></tr>" for e in evs[:10])
    ev_tbl = (f"<table><tr><th class='l'>日期</th><th class='l'>事件</th><th>重要性</th></tr>{ev_rows}</table>"
              if ev_rows else "<p class='n'>未來 45 天沒有登錄的重大事件。</p>")

    # ── 近 10 個交易日
    last10 = full.iloc[-10:][::-1]
    rec = "".join(
        f"<tr><td class='l'>{i:%m/%d}</td><td class='l'><span class='dot' style='background:{LAMP.get(x.light, '#ccc')}'></span>{fs.LIGHT_ZH.get(x.light, x.light).split(' ')[1] if ' ' in fs.LIGHT_ZH.get(x.light, '') else x.light}</td>"
        f"<td>{x.panic_score:.1f}</td><td>{f(x.fg, '{:.0f}')}</td><td>{x.vix:.2f}</td><td>{f(x.move, '{:.1f}')}</td><td>{f(x.vxn, '{:.1f}')}</td>"
        f"<td>{f(x.ts, '{:.3f}')}</td><td>{f(x.hy, '{:.2f}')}</td><td>{int(x.conf) if pd.notna(x.conf) else 0:+d}</td>"
        f"<td>{x.spx:,.0f}</td><td class='l'>{action(x)}</td></tr>" for i, x in last10.iterrows())
    rec_tbl = ("<table class='sm'><tr><th class='l'>日期</th><th class='l'>燈號</th><th>分數</th><th>F&amp;G</th><th>VIX</th><th>MOVE</th>"
               f"<th>VXN</th><th>VIX÷3M</th><th>HY%</th><th>確認</th><th>S&amp;P</th><th class='l'>建議動作</th></tr>{rec}</table>")

    six = full.iloc[-126:]
    lights = six["light"].to_dict()
    charts = (mini_chart(six["panic_score"], "綜合恐慌分數（近 6 個月；底部色條 = 每日燈號）", "#6250d6", lights)
              + mini_chart(six["spx"], "S&amp;P 500（近 6 個月）", "#2a78d6", lights, fmt="{:,.0f}")
              + mini_chart(six["vix"], "VIX（虛線 = 單獨綠燈門檻 30）", "#d03b3b", lights, fmt="{:.0f}", ref=fs.GREEN_VIX_ALONE, ref_label="VIX 30"))

    dd = sig.get("data_dates") or {}
    stale = [k.upper() for k in (sig.get("stale") or [])] if latest else []
    src = "；".join(f"{k.upper()}：{v}" for k, v in (sig.get("sources") or {}).items()) if latest else "資料庫 data/fear_signal.db"
    status = adv.get("headline", "")
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8"><title>當日報告 {d:%Y-%m-%d}</title>
<style>
@page{{size:A4;margin:11mm 11mm 12mm}}
body{{font-family:"Noto Sans CJK TC","Noto Sans TC",sans-serif;color:#0b0b0b;font-size:9pt;line-height:1.45;margin:0}}
h1{{font-size:17pt;margin:0}}h2{{font-size:11.5pt;margin:12px 0 5px;padding-bottom:3px;border-bottom:2px solid #0b0b0b}}
h3{{font-size:9.5pt;margin:9px 0 4px}}.sub,.n{{color:#52514e;font-size:8pt}}.pg{{page-break-before:always}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:3px 5px;border-bottom:1px solid #e6e5e0;text-align:right;white-space:nowrap;font-size:8.3pt}}
th{{color:#52514e;font-weight:600;background:#fafaf8}}.l{{text-align:left}}.b{{font-weight:700}}
.pos{{color:#1a7f37}}.neg{{color:#c0392b}}small{{color:#52514e;font-size:7.3pt;display:block}}
.sm td,.sm th{{font-size:7.6pt;padding:2.4px 4px}}tr{{page-break-inside:avoid}}tr.hi td{{background:#fff4e5}}
.hero{{display:flex;gap:14px;align-items:center;border:1px solid #e6e5e0;border-left:8px solid {lamp};border-radius:10px;padding:10px 14px;margin:8px 0}}
.lamp{{width:46px;height:46px;border-radius:50%;background:{lamp};flex:none;box-shadow:0 0 0 4px {lamp}22}}
.hero .t{{font-size:13pt;font-weight:700}}.hero .h{{font-size:9.5pt;margin-top:3px}}
.kp{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:6px 0}}.kp div{{background:#f5f5f2;border-radius:8px;padding:6px 9px}}
.kp b{{display:block;font-size:13pt}}.kp small.pos,.kp small.neg{{font-weight:600}}.kp small.pos{{color:#1a7f37}}.kp small.neg{{color:#c0392b}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.kv td{{text-align:left}}.kv td:first-child{{color:#52514e;width:45%}}
ul{{margin:3px 0;padding-left:17px}}li{{margin:2px 0}}.ch{{margin:4px 0 6px}}.ct{{font-size:8.6pt;font-weight:600}}svg{{width:100%;height:auto}}
.ax{{font-size:12px;fill:#52514e}}.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}}
</style></head><body>
<h1>恐慌進場燈號・當日報告</h1>
<div class="sub">資料日 {d:%Y-%m-%d}（{'一二三四五六日'[d.weekday()]}）｜報告產出 {pd.Timestamp.now(tz='Asia/Taipei'):%Y-%m-%d %H:%M}（台北）
{('｜各指標最新資料日：' + '、'.join(f'{k.upper()} {v}' for k, v in dd.items())) if dd and latest else ''}
{('｜<span style="color:#c98500">沿用前值：' + '、'.join(stale) + '</span>') if stale else ''}</div>

<div class="hero"><div class="lamp"></div><div><div class="t">{fs.LIGHT_ZH.get(L, L)}｜綜合恐慌分數 {r['panic_score']:.1f}｜確認分數 {conf:+d}</div>
<div class="h">{status}</div></div></div>

<h2>一、今日指標</h2>
<div class="kp">{kp}</div>
<div class="sub">百分位 = 近 252 個交易日中的相對位置（F&amp;G 越低越恐慌；其餘越高越恐慌）。顏色：紅 = 較前日更恐慌／更緊張，綠 = 緩和。</div>

<h2>二、三大指數</h2>
{idx_tbl}

<h2>三、進場計畫與部位建議</h2>
<div class="two"><div>{plan}</div>
<div><b>風險與注意</b><ul>{li(adv.get('notes'))}</ul>
{('<b>加碼條件</b><ul>' + li(adv.get('adds')) + '</ul>') if adv.get('adds') else ''}
{('<b>暫停條件</b><ul>' + li(adv.get('pause')) + '</ul>') if adv.get('pause') else ''}</div></div>
<div class="sub">規則：轉綠第一天先等待；分數自本段高點回落 ≥ {fs.TURN_DROP:.0f} 分 → 第 1 批；再度轉綠創新高後回落 ≥ {fs.TURN_DROP:.0f} 分 → 下一批（最多 {fs.MAX_TRANCHES} 批）。
轉折日若為 FOMC／CPI／非農前一交易日，改於事件公布當日收盤執行。</div>

<div class="pg"></div>
<h2>四、近期重大事件</h2>
{ev_tbl}

<h2>五、近 6 個月走勢</h2>
{charts}

<div class="pg"></div>
<h2>六、歷史參考</h2>
{hist}
{ana_tbl}

<h2>七、近 10 個交易日紀錄</h2>
{rec_tbl}
<div class="sub" style="margin-top:8px">資料來源：{src}。本報告為紀律框架的回測與提示，非投資建議；過去報酬不代表未來。</div>
</body></html>"""
    OUT.mkdir(parents=True, exist_ok=True)
    hp, pdf = OUT / f"daily_report_{d:%Y-%m-%d}.html", OUT / f"daily_report_{d:%Y-%m-%d}.pdf"
    hp.write_text(html, encoding="utf-8")
    chrome = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)
    subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", hp.as_uri()], check=True, capture_output=True)
    hp.unlink(missing_ok=True)
    if shutil.which("qpdf"):                         # 壓縮（約減半），每天提交到 GitHub 時較省空間
        tmp = pdf.with_suffix(".tmp.pdf")
        if subprocess.run(["qpdf", "--object-streams=generate", "--compression-level=9", "--recompress-flate",
                           str(pdf), str(tmp)], capture_output=True).returncode in (0, 3):
            tmp.replace(pdf)
    for old in sorted(OUT.glob("daily_report_*.pdf"))[:-KEEP]:   # 封存只留最近 KEEP 份
        old.unlink()
    if latest:
        (ROOT / "docs").mkdir(exist_ok=True)
        shutil.copy(pdf, ROOT / "docs" / "daily_report.pdf")
    print("PDF：", pdf)
    return pdf


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
