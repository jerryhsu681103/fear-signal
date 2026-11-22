#!/usr/bin/env python3
"""綜合恐慌分數 × 指數 對照線圖（PDF）。
用法： python build_score_charts.py [起年] [迄年]   預設 2025 2026 → reports/score_vs_index_2025_2026.pdf
每個指數一頁：上圖為指數收盤（綠／紅底色 = 綠燈／紅燈日），下圖為綜合恐慌分數，共用時間軸（不使用雙 y 軸）。
"""
import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
for f in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",):
    if Path(f).exists():
        font_manager.fontManager.addfont(f)
plt.rcParams.update({
    "font.family": ["Noto Sans CJK TC", "Noto Sans CJK JP", "PingFang TC", "Microsoft JhengHei", "sans-serif"],
    "axes.edgecolor": "#c9c8c2", "axes.labelcolor": "#52514e", "xtick.color": "#52514e", "ytick.color": "#52514e",
    "axes.grid": True, "grid.color": "#e6e5e0", "grid.linewidth": 0.8, "axes.spines.top": False,
    "axes.spines.right": False, "font.size": 9,
})
INK, INK2 = "#0b0b0b", "#52514e"
SCORE_C = "#6250d6"
IDX = {"spx": ("S&P 500", "#2a78d6"), "ndx": ("納指100", "#1baf7a"), "sox": ("費城半導體", "#eb6834")}
LIGHT_BG = {"GREEN": ("#0ca30c", .18), "RED": ("#d03b3b", .16), "RED2": ("#8e1b1b", .22)}


def shade(ax, df):
    """把燈號日以底色標出（連續同燈號日合併成一段）。"""
    lt = df["light"].values
    i = 0
    while i < len(df):
        if lt[i] in LIGHT_BG:
            j = i
            while j + 1 < len(df) and lt[j + 1] == lt[i]:
                j += 1
            c, a = LIGHT_BG[lt[i]]
            x0 = df.index[i] - pd.Timedelta(hours=14)
            x1 = df.index[j] + pd.Timedelta(hours=14)
            ax.axvspan(x0, x1, color=c, alpha=a, lw=0)
            i = j + 1
        else:
            i += 1


def quintile_table(df, k):
    """依綜合分數分 5 組，計算該期間各組的後續報酬。"""
    bins = [0, 20, 40, 60, 80, 101]
    labs = ["0-20 貪婪", "20-40", "40-60", "60-80", "80-100 恐慌"]
    cat = pd.cut(df["panic_score"], bins=bins, labels=labs, right=False)
    rows = []
    for lab in labs:
        sub = df[cat == lab]
        r = [len(sub)]
        for h in ("2w", "1m", "3m"):
            x = sub[f"{k}_fwd{h}"].dropna()
            r.append(f"{x.mean()*100:+.1f}%（{(x > 0).mean()*100:.0f}%）" if len(x) else "—")
        rows.append([lab] + r)
    x = df[[f"{k}_fwd{h}" for h in ("2w", "1m", "3m")]]
    rows.append(["全期間"] + [len(df)] + [f"{x[c].mean()*100:+.1f}%（{(x[c].dropna() > 0).mean()*100:.0f}%）" for c in x])
    return rows


def yr(y0, y1):
    return f"{y0}" if y0 == y1 else f"{y0}–{y1}"


def page(pdf, df, k, y0, y1):
    name, col = IDX[k]
    fig = plt.figure(figsize=(11.69, 8.27))
    gs = fig.add_gridspec(4, 1, height_ratios=[2.2, 1.25, 0.28, 1.05], hspace=0.1, left=0.07, right=0.97, top=0.9, bottom=0.03)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    fig.suptitle(f"{name} 與綜合恐慌分數（{yr(y0, y1)}）", x=0.07, ha="left", fontsize=15, fontweight="bold", color=INK)
    ret = df[k].iloc[-1] / df[k].iloc[0] - 1
    fig.text(0.07, 0.915, f"{df.index[0]:%Y/%m/%d}–{df.index[-1]:%Y/%m/%d}｜期間漲跌 {ret*100:+.1f}%｜"
             "綠底 = 綠燈（進場）日、紅底 = 紅燈（過熱）日｜上下兩圖共用時間軸", fontsize=9, color=INK2)

    shade(ax1, df)
    ax1.plot(df.index, df[k], color=col, lw=1.8)
    ax1.set_ylabel(f"{name} 收盤")
    ax1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax1.tick_params(labelbottom=False)
    # 標註綠燈段的起點與 1 個月後報酬
    g = df[df["light"] == "GREEN"]
    if len(g):
        pos = np.searchsorted(df.index, g.index)
        grp = np.concatenate([[0], np.cumsum(np.diff(pos) > 10)])
        offs = [(-34, "center"), (26, "center"), (-62, "center")]
        last_x = None
        n = 0
        for _, e in g.groupby(grp):
            d0 = e.index[0]
            if last_x is not None and (d0 - last_x).days < 90:
                n += 1
            else:
                n = 0
            last_x = d0
            dy, ha = offs[n % len(offs)]
            r1 = e[f"{k}_fwd1m"].iloc[0]
            lab = f"{d0:%y/%m/%d} 綠燈\n1個月 {r1*100:+.1f}%" if pd.notna(r1) else f"{d0:%y/%m/%d} 綠燈\n（未滿期）"
            ax1.scatter([d0], [df.loc[d0, k]], s=26, color="#0ca30c", zorder=5, edgecolor="white", lw=1.5)
            ax1.annotate(lab, (d0, df.loc[d0, k]), xytext=(0, dy), textcoords="offset points", ha=ha,
                         fontsize=8, color=INK, arrowprops=dict(arrowstyle="-", color="#8a8984", lw=0.8))

    shade(ax2, df)
    ax2.axhspan(80, 100, color="#0ca30c", alpha=.07, lw=0)
    ax2.axhspan(0, 20, color="#d03b3b", alpha=.06, lw=0)
    ax2.plot(df.index, df["panic_score"], color=SCORE_C, lw=1.6)
    ax2.set_ylim(0, 100)
    ax2.set_yticks([0, 20, 40, 60, 80, 100])
    ax2.set_ylabel("綜合恐慌分數")
    ax2.text(df.index[0], 92, "≥80 極度恐慌", fontsize=8, color="#0a7d0a", va="center")
    ax2.text(df.index[0], 9, "≤20 極度貪婪", fontsize=8, color="#b02e2e", va="center")
    ax2.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 3, 5, 7, 9, 11]))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m"))

    # 相關性與分組表
    ax3 = fig.add_subplot(gs[3])
    ax3.axis("off")
    c1 = df["panic_score"].corr(df[f"{k}_fwd1m"])
    c3 = df["panic_score"].corr(df[f"{k}_fwd3m"])
    ax3.text(0, 1.02, f"綜合分數與 {name} 後續報酬（{yr(y0, y1)}，括號為上漲機率）｜"
             f"分數與 1 個月後報酬相關係數 {c1:+.2f}、與 3 個月 {c3:+.2f}",
             fontsize=9.5, fontweight="bold", color=INK, transform=ax3.transAxes, va="bottom")
    rows = quintile_table(df, k)
    tb = ax3.table(cellText=[[str(c) for c in r] for r in rows],
                   colLabels=["綜合分數分組", "天數", "持有 2 週", "持有 1 個月", "持有 3 個月"],
                   cellLoc="right", colLoc="right", loc="upper left", bbox=[0, 0, 1, 0.98])
    tb.auto_set_font_size(False)
    tb.set_fontsize(8.5)
    for (r, c), cell in tb.get_celld().items():
        cell.set_edgecolor("#e6e5e0")
        cell.set_linewidth(0.6)
        if r == 0:
            cell.set_facecolor("#fafaf8")
            cell.set_text_props(color=INK2, fontweight="bold")
        if c == 0:
            cell.set_text_props(ha="left")
            cell._loc = "left"
        if r == len(rows):
            cell.set_facecolor("#f5f5f2")
    pdf.savefig(fig)
    plt.close(fig)


def overview(pdf, df, y0, y1, png=None):
    """總覽頁：三個指數以期初 = 100 指數化放同一張圖（同單位，可比較），下方為綜合分數。"""
    fig = plt.figure(figsize=(11.69, 8.27))
    gs = fig.add_gridspec(2, 1, height_ratios=[2, 1.2], hspace=0.1, left=0.07, right=0.97, top=0.88, bottom=0.08)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    fig.suptitle(f"綜合恐慌分數 vs 三大指數（{yr(y0, y1)}）", x=0.07, ha="left", fontsize=16, fontweight="bold", color=INK)
    fig.text(0.07, 0.9, f"上圖：三個指數以 {df.index[0]:%Y/%m/%d} = 100 指數化（同一刻度可直接比較）；下圖：綜合恐慌分數。"
             "綠底 = 綠燈日。", fontsize=9, color=INK2)
    shade(ax1, df)
    ends = []
    for k, (name, col) in IDX.items():
        s = df[k] / df[k].iloc[0] * 100
        ax1.plot(df.index, s, color=col, lw=1.8, label=name)
        ends.append([s.iloc[-1], f"{name} {s.iloc[-1]:.0f}", col])
    ends.sort()
    lo_, hi_ = ax1.get_ylim()
    gap = (hi_ - lo_) * 0.045
    ypos = []
    for v, *_ in ends:                                   # 由低到高，標籤至少相隔 gap，避免重疊
        ypos.append(v if not ypos else max(v, ypos[-1] + gap))
    for e, y in zip(ends, ypos):
        e.append(y)
    for e in ends:
        ax1.annotate(e[1], (df.index[-1], e[0]), xytext=(df.index[-1] + pd.Timedelta(days=6), e[3]), textcoords="data",
                     fontsize=8.5, color=INK, va="center", arrowprops=dict(arrowstyle="-", color=e[2], lw=0.8))
    ax1.axhline(100, color="#8a8984", lw=0.8, ls="--")
    ax1.set_ylabel(f"指數化（{df.index[0]:%Y/%m/%d} = 100）")
    ax1.legend(loc="upper left", frameon=False, ncol=3)
    ax1.tick_params(labelbottom=False)
    ax1.set_xlim(df.index[0], df.index[-1] + pd.Timedelta(days=45))
    shade(ax2, df)
    ax2.axhspan(80, 100, color="#0ca30c", alpha=.07, lw=0)
    ax2.axhspan(0, 20, color="#d03b3b", alpha=.06, lw=0)
    ax2.plot(df.index, df["panic_score"], color=SCORE_C, lw=1.6)
    ax2.set_ylim(0, 100)
    ax2.set_yticks([0, 20, 40, 60, 80, 100])
    ax2.set_ylabel("綜合恐慌分數")
    ax2.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 3, 5, 7, 9, 11]))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m"))
    if png:
        fig.savefig(png, dpi=130)                       # 網頁用預覽圖
    pdf.savefig(fig)
    plt.close(fig)


def main(y0=2025, y1=2026, png=None):
    con = sqlite3.connect(ROOT / "data" / "fear_signal.db")
    full = pd.read_sql("select * from daily", con, parse_dates=["date"]).set_index("date")
    df = full[(full.index.year >= y0) & (full.index.year <= y1)].copy()
    out = ROOT / "reports" / f"score_vs_index_{y0}_{y1}.pdf"
    out.parent.mkdir(exist_ok=True)
    with PdfPages(out) as pdf:
        overview(pdf, df, y0, y1, png)
        for k in ("ndx", "spx", "sox"):
            page(pdf, df, k, y0, y1)
        d = pdf.infodict()
        d["Title"] = f"綜合恐慌分數 vs 納指100／S&P500／費城半導體 {y0}-{y1}"
    print("PDF：", out)
    return (out, png) if png else out


if __name__ == "__main__":
    a = [int(x) for x in sys.argv[1:3]]
    main(*a) if a else main()
