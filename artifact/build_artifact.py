"""Build the Fear-Signal control-panel artifact from the program's outputs.
用法： python artifact/build_artifact.py [輸出資料夾，預設 artifact/site]
之後把輸出資料夾內的檔案發佈到控制台成品（slug 1ca35a3e-b979-4966-bd93-40b30ec4bf42）。
"""
import json, re, sqlite3, zipfile, shutil, sys
from pathlib import Path
import pandas as pd

HERE = Path(__file__).resolve().parent
SRC = HERE.parent                                   # repo 根目錄
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "site"
sys.path.insert(0, str(HERE))
if OUT.exists():
    shutil.rmtree(OUT)
(OUT / "files").mkdir(parents=True)

# ---------- data for in-page calculation
df = pd.read_csv(SRC / "data/daily.csv", index_col=0, parse_dates=True)
import math
nz = lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 2)
rows = [[d.strftime("%Y-%m-%d"), nz(r.fg), nz(r.vix), nz(r.move), nz(r.spx), nz(r.ndx), nz(r.sox)]
        for d, r in df.iterrows()]
con = sqlite3.connect(SRC / "data/fear_signal.db")
bt = pd.read_sql('select * from "stats_燈號回測"', con).set_index("分組")
light_key = {"🟢 綠燈 可進場": "GREEN", "🟡 黃燈 恐慌升溫・等待": "YELLOW", "⚪ 灰燈 無訊號": "NEUTRAL", "全部 (基準)": "ALL"}
stats = {}
for lab, r in bt.iterrows():
    k = light_key.get(lab)
    if not k:
        continue
    stats[k] = {"days": int(r["天數"]), "ep": int(r["事件數"])}
    for n, code in (("S&P 500", "spx"), ("納指100", "ndx"), ("費城半導體", "sox")):
        for h in ("2w", "1m", "3m"):
            stats[k][f"{code}_{h}"] = [round(float(r[f"{n}_{h}_平均"]), 4), round(float(r[f"{n}_{h}_勝率"]), 4)]
sig = json.loads((SRC / "data/signal.json").read_text(encoding="utf-8"))
import update_history
update_history.main(str(OUT / "history.json"), str(SRC / "data/seed_macro.csv"), str(SRC / "data/manual_overrides.csv"))

# ---------- program zip (code + database) and PDF
PKG = {"fear_signal.py": "fear_signal.py", "build_pdf_report.py": "build_pdf_report.py",
       "requirements.txt": "requirements.txt", "README.md": "README.md",
       ".github/workflows/daily.yml": "daily.yml", "data/manual_overrides.csv": "manual_overrides.csv",
       "data/daily.csv": "daily.csv", "data/signal.json": "signal.json",
       "data/seed_macro.csv": "seed_macro.csv", "reports.py": "reports.py",
       "build_score_charts.py": "build_score_charts.py", "build_ytd_report.py": "build_ytd_report.py",
       "report_data.py": "report_data.py", ".gitignore": "gitignore",
       "data/event_calendar.csv": "event_calendar.csv", "build_daily_report.py": "build_daily_report.py"}
(OUT / "pkg").mkdir()
shutil.copy(HERE / "update_history.py", OUT / "pkg" / "update_history.py.txt")
for src, name in PKG.items():
    shutil.copy(SRC / src, OUT / "pkg" / (name + ".txt"))
(OUT / "pkg/manifest.json").write_text(json.dumps({v + ".txt": k for k, v in PKG.items()}), encoding="utf-8")
shutil.copy(next(f for f in (SRC / "docs/report.pdf", SRC / "reports/fear_signal_10y_report.pdf") if f.exists()), OUT / "files/report.pdf")
for f in ["daily_report.pdf", "score_charts.pdf", "score_overview.png"] + [p.name for p in SRC.glob("docs/ytd_*.pdf")]:
    shutil.copy(SRC / "docs" / f, OUT / "files" / f)
for f in SRC.glob("reports/yearly/*年度燈號回測報告.pdf"):
    shutil.copy(f, OUT / "files" / f"yearly_{f.name[:4]}.pdf")

# ---------- page: control panel + the program's own report page
base = (SRC / "docs/index.html").read_text(encoding="utf-8")
base = re.sub(r'<div class="card" style="text-align:center"><a href="report.pdf".*?</div>\n', "", base)
base = base.replace("<h1>恐慌進場燈號 Fear-Signal</h1>", '<h2 style="margin-top:28px">資料庫報告</h2>')
base = base.replace("<title>恐慌進場燈號 Fear-Signal</title>", "<title>恐慌進場燈號 Fear-Signal</title>")
panel = (HERE / "panel.html").read_text(encoding="utf-8").replace("__DAILY_DATE__", sig["date"])
base = re.sub(r'href="((?:daily_report|report|score_charts|ytd_\d{4})\.pdf)"', r'href="files/\1" download', base)
page = base.replace("<main>", "<main>" + panel, 1)
page = page.replace("</body>", (HERE / "panel.js").read_text(encoding="utf-8") + "</body>")
(OUT / "index.html").write_text(page, encoding="utf-8")
print("built", [p.name for p in OUT.rglob("*") if p.is_file()], len(rows))
