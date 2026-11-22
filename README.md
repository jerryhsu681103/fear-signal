# 恐慌進場燈號 Fear-Signal

每天自動抓取 **CNN 恐懼貪婪指數、VIX、MOVE**，比對過去 10 年歷史，
統計同類恐慌情緒後 **2 週／1 個月／3 個月** 的 **S&P 500、納指100、費城半導體** 報酬，
並每天輸出燈號網頁與完整 PDF 報告。

## 檔案
| 檔案 | 內容 |
|---|---|
| `fear_signal.py` | 主程式（抓資料 → 建庫 → 統計 → 燈號 → 網頁 → 推播 → PDF） |
| `build_pdf_report.py` | 10 年主報告 PDF 產生器 |
| `build_score_charts.py` | 綜合分數 vs 納指100／S&P 500／費城半導體 線圖 PDF（不用雙 y 軸：上下兩圖共用時間軸） |
| `build_ytd_report.py` | 某一年每日燈號與事後報酬回測 PDF |
| `reports.py` | 報告產生器：用現有資料庫產出上述報告，不需重新抓資料 |
| `reports/fear_signal_10y_report.pdf` | 完整 PDF 報告（每日更新） |
| `data/fear_signal.db` | SQLite 資料庫：`daily` 每日資料、`stats_*` 統計表、`analogs` 相似歷史時點 |
| `data/daily.csv` | 每日資料（同 daily 表，可用 Excel 開） |
| `data/signal.json` | 今日燈號 |
| `docs/index.html` | 手機版燈號網頁（GitHub Pages），頁面上有 PDF 下載連結（`docs/report.pdf`） |

## GitHub 自動更新（已部署）
- Repo：https://github.com/jerryhsu681103/fear-signal
- 排程：每個美股交易日收盤後自動執行（台北時間 週二～週六 06:37），也可在 **Actions → daily-fear-signal → Run workflow** 手動執行。
- 每天提交：資料庫（`data/`）、燈號網頁（`docs/index.html`）、線圖預覽（`docs/score_overview.png`）。
- 每週五或手動執行：另外提交所有 PDF 報告（`docs/*.pdf`、`reports/`）並更新今年度獨立報告，避免每天提交數 MB 的 PDF 讓 repo 過度膨脹。
- 網頁：Settings → Pages 設定 Branch `main`、資料夾 `/docs` 後，網址為 https://jerryhsu681103.github.io/fear-signal/

### 綠燈推播到 iPhone（選用）
1. iPhone 安裝 **ntfy** App，訂閱一個不易猜的 topic 名稱（例如 `jerry-fear-8372`）。
2. repo **Settings → Secrets and variables → Actions → New secret**：名稱 `NTFY_TOPIC`，值填同一個 topic。
3. 燈號「轉為綠燈」當天會收到推播。

## 新增總經指標與部位建議
| 指標 | 來源（依序備援） | 用途 |
|---|---|---|
| VXN | Yahoo ^VXN → FRED VXNCLS → GitHub 鏡像 | ≥40 時確認分數 +1 |
| VIX÷VIX3M | Yahoo ^VIX3M → FRED VXVCLS → 鏡像 | 連續倒掛 ≥3 日 +1；≥1.10 提醒短線急殺 |
| 高收益債利差 | FRED BAMLH0A0HYM2（僅近 3 年）＋ `data/seed_macro.csv` 歷史 | ≥5% +1；<5% 且 5 日擴大 ≥0.3 −1 |
| 10 年實質利率 | FRED DFII10 → 鏡像 | 估值背景（回測非有效時點訊號） |

綠燈時依確認分數動用「機會資金」（目前現金 − 最低現金水位）：≤0 → 20%、+1 → 33%、≥+2 → 50%。
加碼條件（各再 1/3）：VIX÷VIX3M 回到 1 以下／高收益債利差破 5% 後停止擴大／標普自首批再跌 5%。
暫停條件：高收益債利差 ≥4.5% 且 5 日擴大 ≥0.5。門檻在 `fear_signal.py` 開頭可調整。

## 資料延遲與手動補值
- 網頁與 PDF 會標示各指標的「實際最新資料日」；若某指標尚未更新（例如 MOVE 較晚公布），會顯示「沿用前值」。
- 想手動補上最新值，在 `data/manual_overrides.csv` 加一行，例如：`2026-11-20,move,98.5`（series 可填 fg / vix / move），之後執行會以手動值為準。

## 進場時點與重大事件
- **綠燈第一天先等待**：追蹤綠燈後的綜合分數高點，分數自高點回落 ≥ 5 分 → **第 1 批**（動用機會資金 20%／33%／50%，依確認分數）。
- 之後須**再度出現綠燈並創新高 → 再回落 ≥ 5 分** → 下一批（各 1/3，每段最多 3 批）。最後一個綠燈日後 20 個交易日沒有新綠燈，該段結束。
- 回測（10 年、19 段）：綠燈首日進場 1 個月後 S&P 平均 +0.0%、勝率 56%；第 1 批（轉折後）+2.8%、78%；全部進場訊號日 +4.4%、85%。
- **重大事件**（`data/event_calendar.csv`）：進場訊號日的下一個交易日若有 FOMC 利率決議／CPI／非農（高重要度），改在事件公布當日收盤執行。
  綜合分數 ≥ 80 時，FOMC 決議日 S&P 平均波動為一般交易日的 1.9 倍。可自行加入政策事件（type = POLICY、importance = 高）。
- 推播：轉綠燈（準備進場）、**每一批進場訊號**、轉紅燈／深紅時通知。

## 燈號規則
| 燈號 | 條件 |
|---|---|
| 🟢 綠燈 準備進場（等待轉折） | (F&G ≤ 25 且 VIX ≥ 25) 或 VIX ≥ 30 |
| 🟡 黃燈 恐慌升溫 | 綜合恐慌分數 ≥ 60、或 F&G ≤ 25、或 VIX ≥ 25 |
| 🔴 紅燈 過熱・退場警示 | F&G ≥ 75 且（VIX < 15 或 高收益債利差一年百分位 ≤ 20） |
| 🔴 深紅 持續過熱・強烈退場 | 紅燈 且（F&G ≥ 75 連續 ≥ 10 日 或 F&G ≥ 85） |
| ⚪ 灰燈 無訊號 | 其他 |

紅燈不是立即賣出訊號（紅燈後 1 個月 S&P 仍約 7 成上漲），風險集中在 3 個月：深紅後 3 個月 S&P 平均 −3.1%、上漲機率 48%。
建議：紅燈減碼高 beta／槓桿約 1/3、停止加碼；深紅再減 1/3；F&G 跌回 60 以下或 VIX 升破 20 解除。
推播（NTFY_TOPIC）在轉綠燈、轉紅燈、轉深紅時都會通知。

綜合恐慌分數 = F&G（反向）、VIX、MOVE 各自在過去 252 交易日的百分位，等權平均。
觀察期間：2 週 = 10、1 個月 = 21、3 個月 = 63 個交易日（`HORIZONS` 可調整）。
門檻與權重都在 `fear_signal.py` 開頭「參數設定」區，可自行調整。

## 報告
每天自動更新時會一併產出（也可隨時手動產出）：
| 報告 | 指令 | 網頁下載檔名 |
|---|---|---|
| 10 年主報告 | `python reports.py --kind main` | `docs/report.pdf` |
| 綜合分數 vs 三大指數線圖 | `python reports.py --kind charts --from 2025 --to 2026` | `docs/score_charts.pdf`、`docs/score_overview.png` |
| 逐日燈號回測（每年一份） | `python reports.py --kind ytd --from 2025 --to 2026` | `docs/ytd_2025.pdf`、`docs/ytd_2026.pdf` |
| 年度獨立報告（逐日回測＋線圖，每年一個檔案） | `python reports.py --kind yearly --from 2017 --to 2026` | `reports/yearly/<年>年度燈號回測報告.pdf` |
| 全部（主報告＋線圖＋近兩年逐日回測） | `python reports.py` | — |

10 年主報告第八章「逐年燈號回測」彙整每一年的漲跌、燈號天數、綠燈／紅燈是否有效與 10 年燈號日曆。

主程式選項：`python fear_signal.py --reports main,charts` 只產出指定報告；`--chart-from 2023` 改變線圖與逐日回測的起始年；`--no-pdf` 不產出報告。

## 報告資料庫
每次產出報告後，內容會自動寫入資料庫（也可手動執行 `python report_data.py`）：

| 資料庫 | 表格 | 內容 |
|---|---|---|
| `data/fear_signal.db` | `daily` | 每日原始數據、百分位、事後報酬、燈號、確認分數 |
| | `stats_*` | 各指標分組統計（主報告第三～七章） |
| | `report_yearly_summary` | 逐年燈號回測（主報告第八章） |
| | `report_signal_episodes` | 10 年所有綠燈／紅燈段落與事後報酬 |
| | `report_score_bands` | 綜合分數五分組 × 指數 × 持有期間（10 年、近兩年、各年度） |
| | `report_daily_actions` | 每日燈號與建議動作（年度報告第五節） |
| | `report_catalog` | 已產出報告目錄（檔名、期間、頁數、大小、SHA-256） |
| `data/report_archive.db` | `report_files` | PDF 檔案本體（BLOB），每個檔名保留最新一版 |

查詢範例：
```sql
-- 歷年綠燈是否有效
SELECT year, green_spx_1m, year_spx_1m, green_verdict, red_verdict FROM report_yearly_summary;
-- 綜合分數 ≥ 80 時，納指 3 個月後表現（近兩年）
SELECT * FROM report_score_bands WHERE period='2025-2026' AND index_code='ndx' AND horizon='3m';
```
```python
# 從封存資料庫取出某份 PDF
import sqlite3; con = sqlite3.connect("data/report_archive.db")
pdf = con.execute("select pdf from report_files where name=?", ("2020年度燈號回測報告.pdf",)).fetchone()[0]
open("2020.pdf", "wb").write(pdf)
```

## 本機執行
```bash
pip install -r requirements.txt
python fear_signal.py            # 含 PDF（需安裝 Google Chrome 與中文字型）
python fear_signal.py --no-pdf   # 只更新燈號與資料庫
```
