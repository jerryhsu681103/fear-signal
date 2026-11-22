#!/usr/bin/env python3
"""報告產生器：用現有資料庫（data/fear_signal.db）產出各種報告，不重新抓資料。

用法：
    python reports.py                       # 全部：10 年主報告 + 分數線圖 + 今年逐日回測
    python reports.py --kind charts --from 2023 --to 2026     # 只產出 2023–2026 綜合分數 vs 三大指數線圖
    python reports.py --kind ytd --from 2024 --to 2026        # 2024、2025、2026 每年一份逐日燈號回測
    python reports.py --kind main                              # 只產出 10 年主報告
    python reports.py --kind daily                             # 只產出當日報告（最新資料日）
    python reports.py --kind yearly --from 2017 --to 2026     # 每個年度一個獨立 PDF（逐日回測＋線圖）
    （每次產出後，報告數據自動寫入 data/fear_signal.db，PDF 封存至 data/report_archive.db）

產出位置：reports/（完整檔名）＋ docs/（供 GitHub Pages 網頁下載的固定檔名）
    10 年主報告      reports/fear_signal_10y_report.pdf      → docs/report.pdf
    分數 vs 指數線圖  reports/score_vs_index_<起>_<迄>.pdf     → docs/score_charts.pdf、docs/score_overview.png
    逐日燈號回測      reports/ytd_signal_<年>.pdf              → docs/ytd_<年>.pdf
    當日報告          reports/daily/daily_report_<日期>.pdf    → docs/daily_report.pdf
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
KINDS = ("main", "charts", "ytd", "daily")
YEARLY_DIR = ROOT / "reports" / "yearly"


def yearly_report(y: int) -> Path:
    """單一年度完整報告 = 該年每日燈號回測 + 該年綜合分數 vs 三大指數線圖，合併成一個 PDF。"""
    import build_score_charts
    import build_ytd_report
    from pypdf import PdfWriter
    YEARLY_DIR.mkdir(parents=True, exist_ok=True)
    ytd = build_ytd_report.main(y)
    charts = build_score_charts.main(y, y)
    out = YEARLY_DIR / f"{y}年度燈號回測報告.pdf"
    w = PdfWriter()
    for f in (ytd, charts):
        w.append(str(f))
    w.add_metadata({"/Title": f"{y} 年度燈號回測報告"})
    with open(out, "wb") as fh:
        w.write(fh)
    charts.unlink(missing_ok=True)
    if y < pd.Timestamp.today().year - 1:       # 較早年度的逐日回測已併入年度報告，不另外保留
        ytd.unlink(missing_ok=True)
    return out


def make_reports(kinds=KINDS, y0: int | None = None, y1: int | None = None) -> dict[str, list[Path]]:
    """依序產出指定報告；任一份失敗不影響其他份。回傳 {種類: [檔案路徑]}。"""
    this_year = pd.Timestamp.today().year
    y1 = y1 or this_year
    y0 = y0 or (y1 - 1)
    DOCS.mkdir(exist_ok=True)
    out: dict[str, list[Path]] = {}
    for kind in kinds:
        try:
            if kind == "main":
                import build_pdf_report
                pdf = build_pdf_report.main()          # 內部已複製到 docs/report.pdf
                out[kind] = [pdf] if pdf else []
            elif kind == "charts":
                import build_score_charts
                pdf, png = build_score_charts.main(y0, y1, png=DOCS / "score_overview.png")
                shutil.copy(pdf, DOCS / "score_charts.pdf")
                out[kind] = [pdf, png]
            elif kind == "daily":
                import build_daily_report
                out[kind] = [build_daily_report.main()]   # 內部已複製到 docs/daily_report.pdf
            elif kind == "yearly":
                out[kind] = [yearly_report(y) for y in range(y0, y1 + 1)]
            elif kind == "ytd":
                import build_ytd_report
                files = []
                for y in range(y0, y1 + 1):
                    pdf = build_ytd_report.main(y)
                    shutil.copy(pdf, DOCS / f"ytd_{y}.pdf")
                    files.append(pdf)
                out[kind] = files
        except Exception as e:  # noqa: BLE001
            print(f"報告 {kind} 產生失敗：", e)
            out[kind] = []
    # 報告內容寫入資料庫：數據表進 fear_signal.db，PDF 本體進 report_archive.db
    try:
        import report_data
        n = report_data.save_to_db()
        print("報告已寫入資料庫：", n)
    except Exception as e:  # noqa: BLE001
        print("報告寫入資料庫失敗：", e)
    return out


def main():
    ap = argparse.ArgumentParser(description="用現有資料庫產出報告")
    ap.add_argument("--kind", default="all", help="main / charts / ytd / daily / yearly / all，可用逗號組合，例如 charts,ytd")
    ap.add_argument("--from", dest="y0", type=int, help="起始年（線圖與逐日回測用，預設去年）")
    ap.add_argument("--to", dest="y1", type=int, help="結束年（預設今年）")
    a = ap.parse_args()
    kinds = KINDS if a.kind == "all" else tuple(k.strip() for k in a.kind.split(","))
    res = make_reports(kinds, a.y0, a.y1)
    for k, files in res.items():
        for f in files:
            print(f"{k:7s} → {f}")


if __name__ == "__main__":
    main()
