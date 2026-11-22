#!/usr/bin/env python3
"""今年以來每日燈號回測報告（PDF）。
用法： python build_ytd_report.py [年份]   → reports/ytd_signal_<年份>.pdf
依 fear_signal.py 的燈號、確認分數與部位規則，逐日列出訊號與事後實際報酬。
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
from build_pdf_report import p, cls, chart

ROOT = Path(__file__).resolve().parent
IDX = fs.INDICES
HZ = fs.HORIZONS
SHORT = {"GREEN": "綠", "YELLOW": "黃", "NEUTRAL": "灰", "RED": "紅", "RED2": "深紅", "NA": "—"}


def action(r) -> str:
    L, c = r["light"], int(r["conf"]) if pd.notna(r["conf"]) else 0
    if L == "GREEN":
        d = fs.DEPLOY["conf>=2" if c >= 2 else "conf=1" if c == 1 else "conf<=0"]
        return f"動用機會資金 {d:.0%}"
    return {"YELLOW": "等待綠燈", "NEUTRAL": "維持配置", "RED": "停止加碼・減高 beta 1/3",
            "RED2": "再減碼 1/3"}.get(L, "—")


def stat_block(df, mask_name_pairs):
    """各燈號在本年度的實際後續報酬（只計已滿期的日子）。"""
    head = ("<tr><th class='l' rowspan='2'>分組</th><th rowspan='2'>天數</th>"
            + "".join(f"<th colspan='3' class='grp'>持有 {hz}</th>" for _, hz in HZ.values()) + "</tr><tr>"
            + "".join(f"<th>{n}</th>" for n in ("S&amp;P", "納指", "半導體")) * len(HZ) + "</tr>")
    rows = []
    for name, m in mask_name_pairs:
        sub = df[m]
        cells = ""
        for h in HZ:
            for k in IDX:
                r = sub[f"{k}_fwd{h}"].dropna()
                if len(r):
                    cells += (f"<td class='{cls(r.mean())}'><b>{p(r.mean())}</b><br>"
                              f"<small>勝率 {(r > 0).mean()*100:.0f}%｜n={len(r)}</small></td>")
                else:
                    cells += "<td><small>未滿期</small></td>"
        rows.append(f"<tr><td class='l'>{name}</td><td>{len(sub)}</td>{cells}</tr>")
    return f"<table class='full'><thead>{head}</thead>{''.join(rows)}</table>"


def calendar(df) -> str:
    color = fs.LIGHT_COLOR
    rows = []
    for m, g in df.groupby(df.index.month):
        cells = "".join(
            f"<td style='background:{color.get(r.light, '#ddd')};color:#fff;text-align:center' title='{d:%m-%d}'>{d.day}</td>"
            for d, r in g.iterrows())
        rows.append(f"<tr><th class='l'>{m} 月</th>{cells}</tr>")
    return f"<table class='cal'>{''.join(rows)}</table>"


def episodes(df, full) -> str:
    sig = df[df["light"].isin(["GREEN", "RED", "RED2"])]
    if sig.empty:
        return "<p class='n'>本年度沒有綠燈或紅燈。</p>"
    pos = np.searchsorted(df.index, sig.index)
    brk = np.concatenate([[0], np.cumsum((np.diff(pos) > 10) | (sig["light"].values[1:] != sig["light"].values[:-1]))])
    last = full.iloc[-1]
    rows = []
    for _, e in sig.groupby(brk):
        f = e.iloc[0]
        staged = {k: last[k] / (len(e) / (1 / e[k]).sum()) - 1 for k in IDX}
        rows.append(
            f"<tr><td class='l'>{SHORT[f.light]}</td><td class='l'>{e.index[0]:%m-%d}</td><td class='l'>{e.index[-1]:%m-%d}</td>"
            f"<td>{len(e)}</td><td>{int(f.conf):+d}</td><td class='l'>{action(f)}</td>"
            f"<td>{'—' if pd.isna(f.fg) else f'{f.fg:.0f}'}</td><td>{f.vix:.1f}</td>"
            + "".join(f"<td class='{cls(f[f'spx_fwd{h}'])}'>{p(f[f'spx_fwd{h}'])}</td>" for h in HZ)
            + "".join(f"<td class='{cls(last[k]/f[k]-1)} b'>{p(last[k]/f[k]-1)}</td>" for k in IDX)
            + "".join(f"<td class='{cls(v)}'>{p(v)}</td>" for v in staged.values()) + "</tr>")
    head = ("<tr><th class='l' rowspan='2'>燈號</th><th class='l' rowspan='2'>首日</th><th class='l' rowspan='2'>末日</th>"
            "<th rowspan='2'>天數</th><th rowspan='2'>首日<br>確認分數</th><th class='l' rowspan='2'>首日建議</th>"
            "<th rowspan='2'>F&amp;G</th><th rowspan='2'>VIX</th>"
            "<th colspan='3' class='grp'>首日買進 S&amp;P 持有</th><th colspan='3' class='grp'>首日買進至最新收盤</th>"
            "<th colspan='3' class='grp'>該段每個燈號日分批買進至最新收盤</th></tr><tr>"
            + "".join(f"<th>{hz}</th>" for _, hz in HZ.values())
            + "".join(f"<th>{n}</th>" for n in ("S&amp;P", "納指", "半導體")) * 2 + "</tr>")
    return f"<table class='full'><thead>{head}</thead>{''.join(rows)}</table>"


def daily_table(df, full) -> str:
    last = full.iloc[-1]
    head = ("<tr><th class='l' rowspan='2'>日期</th><th rowspan='2'>燈號</th><th rowspan='2'>F&amp;G</th><th rowspan='2'>VIX</th>"
            "<th rowspan='2'>MOVE</th><th rowspan='2'>綜合<br>分數</th><th rowspan='2'>VXN</th><th rowspan='2'>VIX÷<br>VIX3M</th>"
            "<th rowspan='2'>倒掛<br>天數</th><th rowspan='2'>HY<br>%</th><th rowspan='2'>HY<br>5日</th><th rowspan='2'>實質<br>利率</th>"
            "<th rowspan='2'>確認<br>分數</th><th class='l' rowspan='2'>建議動作</th><th rowspan='2'>S&amp;P<br>收盤</th>"
            "<th colspan='3' class='grp'>S&amp;P 事後報酬</th><th colspan='2' class='grp'>半導體 事後報酬</th>"
            f"<th colspan='2' class='grp'>至 {full.index[-1]:%Y/%m/%d}</th></tr><tr>"
            "<th>2週</th><th>1個月</th><th>3個月</th><th>1個月</th><th>3個月</th><th>S&amp;P</th><th>半導體</th></tr>")
    nf = lambda v, f: "—" if pd.isna(v) else f.format(v)
    rows = []
    for d, r in df.iterrows():
        bg = {"GREEN": "#0ca30c22", "YELLOW": "#fab21922", "RED": "#d03b3b22", "RED2": "#8e1b1b33"}.get(r.light, "")
        dot = fs.LIGHT_COLOR.get(r.light, "#ccc")
        rows.append(
            f"<tr style='background:{bg}'><td class='l'>{d:%m-%d}</td>"
            f"<td><span class='dot' style='background:{dot}'></span>{SHORT.get(r.light, '—')}</td>"
            f"<td>{nf(r.fg, '{:.0f}')}</td><td>{nf(r.vix, '{:.1f}')}</td><td>{nf(r.move, '{:.0f}')}</td>"
            f"<td>{nf(r.panic_score, '{:.0f}')}</td><td>{nf(r.vxn, '{:.1f}')}</td><td>{nf(r.ts, '{:.2f}')}</td>"
            f"<td>{int(r.inv_days) or ''}</td><td>{nf(r.hy, '{:.2f}')}</td><td>{nf(r.hy_5d, '{:+.2f}')}</td>"
            f"<td>{nf(r.rr, '{:.2f}')}</td><td>{int(r.conf):+d}</td><td class='l'>{action(r)}</td>"
            f"<td>{r.spx:,.0f}</td>"
            + "".join(f"<td class='{cls(r[f'spx_fwd{h}'])}'>{p(r[f'spx_fwd{h}']) if pd.notna(r[f'spx_fwd{h}']) else '…'}</td>" for h in HZ)
            + "".join(f"<td class='{cls(r[f'sox_fwd{h}'])}'>{p(r[f'sox_fwd{h}']) if pd.notna(r[f'sox_fwd{h}']) else '…'}</td>" for h in ("1m", "3m"))
            + f"<td class='{cls(last.spx/r.spx-1)}'>{p(last.spx/r.spx-1)}</td><td class='{cls(last.sox/r.sox-1)}'>{p(last.sox/r.sox-1)}</td></tr>")
    return f"<table class='full daily'><thead>{head}</thead>{''.join(rows)}</table>"


def main(year: int | None = None):
    con = sqlite3.connect(ROOT / "data" / "fear_signal.db")
    full = pd.read_sql("select * from daily", con, parse_dates=["date"]).set_index("date")
    year = year or full.index[-1].year
    df = full[full.index.year == year]
    sig = json.loads((ROOT / "data" / "signal.json").read_text(encoding="utf-8"))
    cnt = df["light"].value_counts()
    L = lambda k: df["light"] == k
    summary = stat_block(df, [("本年度全部交易日（基準）", df["light"].notna()),
                              ("🟢 綠燈", L("GREEN")), ("🟡 黃燈", L("YELLOW")), ("⚪ 灰燈", L("NEUTRAL")),
                              ("🔴 紅燈＋深紅", df["light"].isin(["RED", "RED2"]))])
    conf_tbl = stat_block(df, [(f"綠燈・確認分數 {c}", L("GREEN") & m) for c, m in
                               (("≤ 0", df["conf"] <= 0), ("+1", df["conf"] == 1), ("≥ +2", df["conf"] >= 2))])
    ytd = {k: df[k].iloc[-1] / df[k].iloc[0] - 1 for k in IDX}
    g = df[L("GREEN")]
    g1m = g["spx_fwd1m"].dropna()
    # 綠燈段落（相隔 > 10 交易日為新的一段）
    eps = []
    if len(g):
        pos = np.searchsorted(df.index, g.index)
        grp = np.concatenate([[0], np.cumsum(np.diff(pos) > 10)])
        eps = [e for _, e in g.groupby(grp)]
    ep_txt = "、".join(f"{e.index[0]:%m/%d}–{e.index[-1]:%m/%d}（{len(e)} 天）" for e in eps) or "無"
    bullets = []
    for e in eps:
        f = e.iloc[0]
        w2 = e["spx_fwd2w"].dropna()
        m1 = e["spx_fwd1m"].dropna()
        txt = (f"<li><b>綠燈 {e.index[0]:%m/%d}–{e.index[-1]:%m/%d}（{len(e)} 天）：</b>首日 F&amp;G {'—' if pd.isna(f.fg) else f'{f.fg:.0f}'}、VIX {f.vix:.1f}、確認分數 {int(f.conf):+d}。")
        if len(w2):
            txt += f"2 週後 S&amp;P 平均 {p(w2.mean())}（最差 {p(w2.min())}）"
        if len(m1):
            txt += f"，1 個月後平均 {p(m1.mean())}、上漲 {(m1 > 0).mean()*100:.0f}%"
        if not len(m1):
            txt += "，尚未滿 1 個月"
        last_px = full.iloc[-1]
        staged = last_px["spx"] / (len(e) / (1 / e["spx"]).sum()) - 1
        txt += f"；該段每個綠燈日分批買進至 {full.index[-1]:%Y/%m/%d}：S&amp;P {p(staged)}。</li>"
        bullets.append(txt)
    early_dip = [e for e in eps if e["spx_fwd2w"].dropna().min() < -0.02] if eps else []
    if len(g):
        green_all = (f"<li><b>綠燈整體：</b>{year} 年 {cnt.get('GREEN', 0)} 個綠燈日（{ep_txt}）。"
                     + (f"已滿 1 個月的 {len(g1m)} 天，S&amp;P 1 個月平均 {p(g1m.mean())}、上漲 {(g1m > 0).mean()*100:.0f}%；"
                        if len(g1m) else "尚未滿 1 個月；")
                     + f"本年度全部交易日為 {p(df['spx_fwd1m'].mean())}／{(df['spx_fwd1m'].dropna() > 0).mean()*100:.0f}%。</li>")
    else:
        green_all = f"<li><b>綠燈：</b>{year} 年沒有出現綠燈（市場未出現足夠的恐慌），規則上沒有進場訊號。</li>"
    rd = df[df["light"].isin(["RED", "RED2"])]
    if len(rd):
        r1, r3, s3 = rd["spx_fwd1m"].dropna(), rd["spx_fwd3m"].dropna(), rd["sox_fwd3m"].dropna()
        b3 = df["spx_fwd3m"].dropna()
        pos_r = np.searchsorted(df.index, rd.index)
        grp_r = np.concatenate([[0], np.cumsum(np.diff(pos_r) > 10)])
        reps = [e for _, e in rd.groupby(grp_r)]
        ep_r = "；".join(
            f"{e.index[0]:%m/%d}–{e.index[-1]:%m/%d}（{len(e)} 天{'，含深紅' if (e['light'] == 'RED2').any() else ''}）"
            + (f"：首日起 3 個月 S&amp;P {p(e['spx_fwd3m'].iloc[0])}、3 個月內最大回檔 {p(e['spx_mdd3m'].iloc[0])}"
               if pd.notna(e['spx_fwd3m'].iloc[0]) else "") for e in reps)
        red_note = (f"<li><b>紅燈（過熱・退場警示）：</b>{len(rd)} 天（深紅 {int((rd['light'] == 'RED2').sum())} 天），"
                    f"1 個月後 S&amp;P 平均 {p(r1.mean()) if len(r1) else '—'}（上漲 {((r1 > 0).mean()*100 if len(r1) else 0):.0f}%），"
                    f"3 個月後 {p(r3.mean()) if len(r3) else '—'}（上漲 {((r3 > 0).mean()*100 if len(r3) else 0):.0f}%）；"
                    f"費城半導體 3 個月 {p(s3.mean()) if len(s3) else '—'}；本年度全部交易日 3 個月 {p(b3.mean())}。</li>"
                    f"<li><b>紅燈段落：</b>{ep_r}。</li>"
                    + (f"<li><b>紅燈判讀：</b>{'本年度紅燈多為假警報，3 個月後市場仍高於一般交易日水準，分批減碼的機會成本較高' if len(r3) and r3.mean() > b3.mean() else '本年度紅燈有效：3 個月後表現低於一般交易日，減碼／停止加碼避開了部分回檔'}。</li>" if len(r3) else ""))
    else:
        red_note = (f"<li><b>紅燈：</b>{year} 年沒有出現 F&amp;G ≥ 75 且波動或信用同時過度樂觀的情況，因此沒有退場訊號。</li>")
    y1, b1 = df[L("YELLOW")]["spx_fwd1m"].mean(), df["spx_fwd1m"].mean()
    if pd.isna(y1):
        y_cmp = "尚未滿期"
    elif y1 - b1 <= -0.01:
        y_cmp = "低於一般交易日，等待綠燈避開了下跌段"
    elif y1 - b1 >= 0.01:
        y_cmp = "高於一般交易日，此年度黃燈階段提前進場會更好"
    else:
        y_cmp = "與一般交易日接近"
    lo = g[g["conf"] <= 0]["spx_fwd1m"].dropna()
    hi = g[g["conf"] >= 1]["spx_fwd1m"].dropna()
    conf_note = ""
    if len(lo) and len(hi):
        better = ("兩者差距不到 1 個百分點，本年度分數的區辨力有限" if abs(hi.mean() - lo.mean()) < 0.01 else
                  "確認分數 ≥ +1 的綠燈較佳，符合 10 年回測") if hi.mean() >= lo.mean() - 0.01 and abs(hi.mean() - lo.mean()) < 0.01 or hi.mean() > lo.mean() else \
                 "本年度確認分數 ≤ 0 的綠燈反而較佳（與 10 年回測相反），代表最低點常出現在信用利差急擴、分數偏低的時候"
        trig = []
        for e in eps:
            win = df.loc[e.index[0]:e.index[-1], "spx"]
            dd = win.min() / win.iloc[0] - 1
            if dd <= -0.05:
                trig.append(f"{e.index[0]:%m/%d} 起的綠燈期間 S&amp;P 最多再跌 {p(dd)}（{win.idxmin():%m/%d}），已觸發「再跌 5% 加碼 1/3」")
        conf_note = (f"<li><b>確認分數：</b>綠燈中分數 ≤ 0 的 {len(lo)} 天 1 個月平均 {p(lo.mean())}，分數 ≥ +1 的 {len(hi)} 天 {p(hi.mean())}；{better}。"
                     + ("；".join(trig) + "，分批加碼規則可補足首批比例偏低的問題。" if trig else "")
                     + (f"（分數 ≥ +1 僅 {len(hi)} 天，樣本太少）" if len(hi) < 5 else "") + "</li>")
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8"><title>{year} 年每日燈號回測</title>
<style>
@page{{size:A4 landscape;margin:10mm 10mm 12mm}}
body{{font-family:"Noto Sans CJK TC","Noto Sans TC",sans-serif;color:#0b0b0b;font-size:9pt;line-height:1.4;margin:0}}
h1{{font-size:18pt;margin:0 0 4px}}h2{{font-size:13pt;margin:10px 0 6px;padding-bottom:3px;border-bottom:2px solid #0b0b0b}}
h3{{font-size:10pt;margin:10px 0 4px}}.sub,.n{{color:#52514e;font-size:8.5pt}}.pg{{page-break-before:always}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:2.5px 4px;border-bottom:1px solid #e6e5e0;text-align:right;white-space:nowrap;font-size:8pt}}
th{{color:#52514e;font-weight:600;background:#fafaf8}}th.grp{{text-align:center}}.l{{text-align:left}}.b{{font-weight:700}}
.pos{{color:#1a7f37}}.neg{{color:#c0392b}}small{{color:#52514e;font-size:7pt}}
.daily td,.daily th{{font-size:7pt;padding:1.6px 3px}}thead{{display:table-header-group}}tr{{page-break-inside:avoid}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:3px;vertical-align:middle}}
.cal td{{width:22px;font-size:7pt;padding:3px 0;border:1px solid #fff}}.cal th{{width:40px;font-size:8pt}}
.kp{{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:8px 0}}.kp div{{background:#f5f5f2;border-radius:8px;padding:7px 9px}}
.kp b{{display:block;font-size:14pt}}.kp small{{display:block}}ul{{margin:4px 0;padding-left:18px}}li{{margin:2px 0}}
.ch{{margin:4px 0}}.ct{{font-size:9pt;font-weight:600}}svg{{width:100%;height:auto}}.ax{{font-size:10px;fill:#52514e}}
</style></head><body>
<h1>{year} 年每日燈號回測（{df.index[0]:%m/%d} ～ {df.index[-1]:%m/%d}，{len(df)} 個交易日）</h1>
<div class="sub">依現行規則逐日計算：綠燈（F&amp;G≤25 且 VIX≥25，或 VIX≥30）／黃燈／灰燈／紅燈（F&amp;G≥75 且 VIX&lt;15 或 HY 利差一年百分位≤20）；
確認分數與動用比例依第六章。報告產出 {pd.Timestamp.now():%Y-%m-%d}。</div>

<div class="kp">
<div><small>🟢 綠燈</small><b>{cnt.get('GREEN', 0)} 天</b><small>{len(eps)} 段</small></div>
<div><small>🟡 黃燈</small><b>{cnt.get('YELLOW', 0)} 天</b></div>
<div><small>⚪ 灰燈</small><b>{cnt.get('NEUTRAL', 0)} 天</b></div>
<div><small>🔴 紅燈／深紅</small><b>{cnt.get('RED', 0) + cnt.get('RED2', 0)} 天</b><small>{f'{year} 年未出現過熱' if not (cnt.get('RED', 0) + cnt.get('RED2', 0)) else ''}</small></div>
<div><small>{year} 年漲跌（{df.index[0]:%m/%d}–{df.index[-1]:%m/%d}）</small><b class="{cls(ytd['spx'])}">S&amp;P {p(ytd['spx'])}</b>
<small>納指 {p(ytd['ndx'])}｜半導體 {p(ytd['sox'])}</small></div></div>

<h2>一、回測結論</h2><ul>
{green_all}
<li style="display:none"><b>綠燈整體：</b>{year} 年 {cnt.get('GREEN', 0)} 個綠燈日（{ep_txt}）。已滿 1 個月的 {len(g1m)} 天，S&amp;P 1 個月平均 {p(g1m.mean()) if len(g1m) else '—'}、上漲 {((g1m > 0).mean()*100 if len(g1m) else 0):.0f}%；
本年度全部交易日為 {p(df['spx_fwd1m'].mean())}／{(df['spx_fwd1m'].dropna() > 0).mean()*100:.0f}%。</li>
{''.join(bullets)}
{('<li><b>一次買 vs 分批：</b>' + '；'.join(f"{e.index[0]:%m/%d} 起的綠燈初期 2 週內最多再跌 {p(e['spx_fwd2w'].dropna().min())}，首日一次買進至 {full.index[-1]:%Y/%m/%d} S&amp;P {p(full.iloc[-1]['spx']/e.iloc[0]['spx']-1)}、分批 {p(full.iloc[-1]['spx']/(len(e)/(1/e['spx']).sum())-1)}" for e in early_dip) + '。</li>') if early_dip else ''}
<li><b>黃燈：</b>{cnt.get('YELLOW', 0)} 天，1 個月平均 {p(y1)}（全部交易日 {p(b1)}），{y_cmp}；規則上不動用資金。</li>
{conf_note}
{red_note}
<li><b>注意：</b>本年度樣本很少，結論以 10 年回測主報告為準；表中「…」表示尚未滿期。</li></ul>

<h2>二、各燈號在 {year} 年的實際後續報酬</h2>
{summary}
<h3>綠燈 × 確認分數</h3>
{conf_tbl}

<div class="pg"></div><h2>三、燈號日曆（每格一個交易日）</h2>
{calendar(df)}
<div class="sub" style="margin:4px 0 8px">綠 = 綠燈、黃 = 黃燈、灰 = 灰燈、紅 = 紅燈、深紅 = 深紅。</div>
{chart(df['spx'], f'S&amp;P 500 {year} 年走勢', float(df['spx'].min()*0.98), float(df['spx'].max()*1.01), fmt='{:,.0f}')}
<h2>四、本年度訊號事件</h2>
{episodes(df, full)}

<div class="pg"></div><h2>五、每日燈號與事後報酬（{len(df)} 天）</h2>
<div class="sub">事後報酬 = 當日收盤買進持有 2 週（10 交易日）／1 個月（21）／3 個月（63）；「…」= 尚未滿期。底色依燈號。</div>
{daily_table(df, full)}
<div class="sub" style="margin-top:6px">資料來源：{'；'.join(f'{k.upper()}：{v}' for k, v in sig['sources'].items())}。本報告為紀律框架回測，非投資建議。</div>
</body></html>"""
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    hp, pdf = out / f"ytd_{year}.html", out / f"ytd_signal_{year}.pdf"
    hp.write_text(html, encoding="utf-8")
    chrome = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)
    subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", hp.as_uri()], check=True, capture_output=True)
    print("PDF：", pdf)
    return pdf


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
