# 恐慌進場燈號 Fear-Signal

每天自動抓取 **CNN 恐懼貪婪指數、VIX、MOVE**，比對過去 10 年歷史，
統計同類恐慌情緒後一個月 **S&P 500、納指100、費城半導體** 的報酬，並輸出燈號網頁。

## 檔案
| 檔案 | 內容 |
|---|---|
| `fear_signal.py` | 主程式（抓資料 → 建庫 → 統計 → 燈號 → 網頁 → 推播） |
| `data/fear_signal.db` | SQLite 資料庫：`daily` 每日資料、`stats_*` 統計表、`analogs` 相似歷史時點 |
| `data/daily.csv` | 每日資料（同 daily 表，可用 Excel 開） |
| `data/signal.json` | 今日燈號 |
| `docs/index.html` | 手機版燈號網頁（GitHub Pages） |

## 部署到 GitHub（約 5 分鐘）
1. GitHub 建立新 repo（例如 `fear-signal`），把本資料夾全部檔案上傳（含 `.github` 資料夾）。
2. **Settings → Actions → General → Workflow permissions** 選 *Read and write permissions*。
3. **Settings → Pages**：Source 選 *Deploy from a branch*，Branch 選 `main`、資料夾 `/docs`。
4. **Actions → daily-fear-signal → Run workflow** 手動跑第一次。
5. 網址：`https://<你的帳號>.github.io/fear-signal/`，iPhone Safari 開啟後「加入主畫面」。

### 綠燈推播到 iPhone（選用）
1. iPhone 安裝 **ntfy** App，訂閱一個不易猜的 topic 名稱（例如 `jerry-fear-8372`）。
2. repo **Settings → Secrets and variables → Actions → New secret**：名稱 `NTFY_TOPIC`，值填同一個 topic。
3. 燈號「轉為綠燈」當天會收到推播。

## 燈號規則
| 燈號 | 條件 |
|---|---|
| 🟢 綠燈 可進場 | (F&G ≤ 25 且 VIX ≥ 25) 或 VIX ≥ 30 |
| 🟡 黃燈 恐慌升溫 | 綜合恐慌分數 ≥ 60、或 F&G ≤ 25、或 VIX ≥ 25 |
| ⚪ 灰燈 無訊號 | 其他 |

綜合恐慌分數 = F&G（反向）、VIX、MOVE 各自在過去 252 交易日的百分位，等權平均。
門檻與權重都在 `fear_signal.py` 開頭「參數設定」區，可自行調整。

## 本機執行
```bash
pip install -r requirements.txt
python fear_signal.py
```
