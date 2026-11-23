<script>
(() => {
  const $ = id => document.getElementById(id);
  const GREEN_FG = 25, GREEN_VIX = 25, GREEN_VIX_ALONE = 30, YELLOW_SCORE = 60, WIN = 252;
  const LIGHT = {GREEN: ["🟢 綠燈 準備進場（等待轉折）", "#0ca30c"], YELLOW: ["🟡 黃燈 恐慌升溫・等待", "#fab219"],
                 NEUTRAL: ["⚪ 灰燈 無訊號", "#8a8984"], RED: ["🔴 紅燈 過熱・退場警示", "#d03b3b"],
                 RED2: ["🔴 深紅 持續過熱・強烈退場", "#8e1b1b"]};
  const HZ = [["2w", "2週"], ["1m", "1個月"], ["3m", "3個月"]];
  const IDX = [["spx", "S&P 500"], ["ndx", "納指100"], ["sox", "費城半導體"]];
  let H = null, entries = {}, db = null, PLAN = null;
  const TURN_DROP = 5, MAX_TR = 3, TAIL = 20;
  // 進場規則：綠燈第一天先等待；分數自高點回落 ≥5 → 第 1 批；再度轉綠創高後回落 ≥5 → 下一批
  function computePlan(S) {
    const n = S.length, ph = Array(n).fill("NONE"), tr = Array(n).fill(0), pk = Array(n).fill(null), done = Array(n).fill(0);
    let active = false, last = -999, k = 0, peak = null, needNew = false, armed = false;
    const lights = S.map((_, i) => i < WIN * 0.8 ? "NA" : evaluateBase(S, i).light), sc = S.map((_, i) => i < WIN * 0.8 ? null : evaluateBase(S, i).score);
    for (let i = 0; i < n; i++) {
      const g = lights[i] === "GREEN";
      if (g) { if (!active) { active = true; k = 0; peak = sc[i]; needNew = false; armed = true; }
               last = i; if (needNew) { needNew = false; armed = true; peak = sc[i]; } }
      if (active && i - last > TAIL) active = false;
      if (!active) continue;
      done[i] = k;
      if (sc[i] != null) peak = peak == null ? sc[i] : Math.max(peak, sc[i]);
      pk[i] = peak;
      if (armed && k < MAX_TR && sc[i] != null && sc[i] <= peak - TURN_DROP) { k++; tr[i] = k; done[i] = k; ph[i] = "BUY"; needNew = true; armed = false; }
      else ph[i] = (armed && k < MAX_TR) ? "WAIT" : (k ? "HOLD" : "WAIT");
    }
    return {ph, tr, pk, done};
  }

  const pct = v => (v == null || isNaN(v)) ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
  const cls = v => (v == null || isNaN(v)) ? "" : (v > 0 ? "pos" : "neg");
  const num = id => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };

  // merge database rows with saved manual entries (manual wins on the same date)
  function series() {
    const map = new Map(H.rows.map(r => [r[0], {date: r[0], fg: r[1], vix: r[2], move: r[3], spx: r[4], ndx: r[5], sox: r[6],
      vxn: r[7], vix3m: r[8], hy: r[9], rr: r[10], hy5: r[11]}]));
    for (const e of Object.values(entries)) map.set(e.date, {...map.get(e.date), ...e, manual: true});
    const out = [...map.values()].sort((a, b) => a.date < b.date ? -1 : 1);
    for (let i = 1; i < out.length; i++)            // carry forward any blanks
      for (const k of ["fg", "vix", "move", "spx", "ndx", "sox", "vxn", "vix3m", "hy", "rr"])
        if (out[i][k] == null || isNaN(out[i][k])) { out[i][k] = out[i - 1][k]; out[i]["ff_" + k] = true; }
    return out;
  }
  function rollPct(arr, i) {                          // same as Python: rank vs previous 251 values
    const prev = arr.slice(Math.max(0, i - WIN + 1), i), v = arr[i];
    if (prev.length < WIN * 0.8 - 1) return null;
    let lt = 0, eq = 0; for (const p of prev) { if (p < v) lt++; else if (p === v) eq++; }
    return (lt / prev.length) * 100 + (eq / prev.length) * 50;
  }
  const EVC = new Map();
  function evaluate(S, i) {
    const ev = evaluateBase(S, i);
    if (PLAN && PLAN.ph.length === S.length) { ev.phase = PLAN.ph[i]; ev.tranche = PLAN.tr[i]; ev.peak = PLAN.pk[i]; ev.done = PLAN.done[i]; }
    return ev;
  }
  function evaluateBase(S, i) {
    const key = S.length + ":" + i + ":" + S[i].date + ":" + (S[i].manual ? JSON.stringify(S[i]) : "");
    if (EVC.has(key)) return EVC.get(key);
    const v = evaluateRaw(S, i); EVC.set(key, v); return v;
  }
  const COLC = new WeakMap();
  function colOf(S, k) { let m = COLC.get(S); if (!m) { m = {}; COLC.set(S, m); } return m[k] || (m[k] = S.map(r => r[k])); }
  function evaluateRaw(S, i) {
    const col = k => colOf(S, k);
    const ffg = 100 - rollPct(col("fg"), i), fvix = rollPct(col("vix"), i), fmove = rollPct(col("move"), i);
    const score = (ffg + fvix + fmove) / 3, r = S[i];
    let light = "NEUTRAL";
    const phy = rollPct(col("hy"), i); let hot = 0; for (let j = i; j >= 0 && S[j].fg >= 75; j--) hot++;
    const isRed = r.fg >= 75 && (r.vix < 15 || (phy != null && phy <= 20));
    if ((r.fg <= GREEN_FG && r.vix >= GREEN_VIX) || r.vix >= GREEN_VIX_ALONE) light = "GREEN";
    else if (isRed) light = (hot >= 10 || r.fg >= 85) ? "RED2" : "RED";
    else if (score >= YELLOW_SCORE || r.fg <= GREEN_FG || r.vix >= GREEN_VIX) light = "YELLOW";
    // 新增總經指標：期限結構、倒掛天數、確認分數
    const tsOf = j => (S[j].vix && S[j].vix3m) ? S[j].vix / S[j].vix3m : null;
    const ts = tsOf(i); let inv = 0; for (let j = i; j >= 0 && tsOf(j) > 1; j--) inv++;
    let hy5 = r.hy5;                                  // 5 個觀測日前的 HY（手動輸入時以前 5 個交易日估算）
    if (r.manual || hy5 == null) { const p = S[Math.max(0, i - 5)]; hy5 = (r.hy != null && p.hy != null) ? r.hy - p.hy : null; }
    const conf = (inv >= 3) + (r.vxn >= 40) + (r.hy >= 5) - ((r.hy < 5 && hy5 >= 0.3) ? 1 : 0);
    return {light, score, ffg, fvix, fmove, r, ts, inv, hy5, conf};
  }
  function upcoming(date, days) {
    const t0 = new Date(date + "T00:00:00"), t1 = new Date(t0.getTime() + days * 864e5);
    return (H.events || []).filter(e => { const d = new Date(e.date + "T00:00:00"); return d > t0 && d <= t1; });
  }
  function prevBiz(ds) { const d = new Date(ds + "T00:00:00"); do { d.setDate(d.getDate() - 1); } while (d.getDay() === 0 || d.getDay() === 6);
    return `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`; }
  function advice(ev) {
    const notes = [], r = ev.r;
    let deploy = 0, head = "無訊號 → 維持原配置";
    const nxt = (ev.done || 0) + 1, dep1 = ev.conf >= 2 ? 0.5 : ev.conf === 1 ? 0.33 : 0.2;
    if (ev.phase === "BUY") {
      head = `第 ${ev.tranche} 批進場訊號：分數自高點 ${ev.peak.toFixed(1)} 回落至 ${ev.score.toFixed(1)} → ` +
             (ev.tranche === 1 ? `動用機會資金 ${Math.round(dep1 * 100)}%（確認分數 ${ev.conf >= 0 ? "+" : ""}${ev.conf}）` : "再動用機會資金 1/3");
      const nb = upcoming(r.date, 4).filter(e => e.importance === "高")[0];
      if (nb) notes.push(`下一個交易日附近有 ${nb.event}（${nb.date.slice(5)}），建議等公布後於該日收盤執行`);
      return {deploy: ev.tranche === 1 ? dep1 : 1 / 3, head, notes: notes.concat(baseNotes(ev))};
    }
    if (ev.phase === "WAIT" || ev.phase === "HOLD") {
      head = ev.phase === "HOLD" && ev.light !== "GREEN"
        ? `已發出 ${ev.done} 批進場訊號，等待再度轉綠並創新高後的轉折（第 ${nxt} 批）`
        : `${ev.light === "GREEN" ? "綠燈・先等待" : "追蹤中"}：分數高點 ${ev.peak.toFixed(1)}，回落至 ≤ ${(ev.peak - TURN_DROP).toFixed(1)} 才發出第 ${nxt} 批進場訊號` +
          (nxt === 1 ? `（屆時動用機會資金 ${Math.round(dep1 * 100)}%）` : "（屆時再動用 1/3）") + (ev.done ? `；本段已發出 ${ev.done} 批` : "");
      const hi = upcoming(r.date, 30).filter(e => e.importance === "高");
      if (hi.length) notes.push("若轉折出現在 " + hi.map(e => `${prevBiz(e.date)}（${e.event}前一日）`).join("、") + "，改在事件公布當日收盤執行");
      return {deploy: 0, head, notes: notes.concat(baseNotes(ev))};
    }
    return adviceOld(ev);
  }
  function baseNotes(ev) { return adviceOld(ev).notes.filter(n => !n.startsWith("過熱") && !n.startsWith("解除")); }
  function adviceOld(ev) {
    const notes = [], r = ev.r;
    let deploy = 0, head = "無訊號 → 維持原配置";
    if (ev.light === "RED" || ev.light === "RED2") {
      head = ev.light === "RED2" ? "深紅・持續過熱 → 停止加碼，分批獲利了結，現金補回基準水位上緣"
                                 : "紅燈・情緒過熱 → 停止加碼，先減碼高波動部位約 1/3，現金補回最低水位以上";
      notes.push("過熱不是立即賣出訊號：紅燈後 1 個月 S&P 仍約 7 成上漲，風險集中在 3 個月內；減碼從半導體／高 beta 開始");
      notes.push("解除：F&G 跌回 60 以下或 VIX 升破 20");
    }
    if (ev.light === "GREEN") { head = "綠燈・先等待分數轉折"; }
    else if (ev.light === "YELLOW") head = "黃燈 → 不動用機會資金，等待綠燈";
    if (ev.ts >= 1.1) notes.push(`VIX÷VIX3M ${ev.ts.toFixed(2)} ≥ 1.10：歷史上 2 週內常再急殺，請至少分 3 批`);
    if (r.hy < 5 && ev.hy5 >= 0.3) notes.push(`高收益債利差 ${r.hy.toFixed(2)}%（5 日 +${ev.hy5.toFixed(2)}）：信用惡化中、尚未出清`);
    if (r.rr >= 2.5) notes.push(`10 年實質利率 ${r.rr.toFixed(2)}%：估值逆風（非進出場訊號）`);
    if (r.hy >= 4.5 && ev.hy5 >= 0.5) notes.push("⚠ 高收益債利差 ≥4.5% 且 5 日擴大 ≥0.5 → 暫停加碼");
    return {deploy, head, notes};
  }
  function macroKpi(ev) {
    const r = ev.r, f = (v, d, u = "") => v == null || isNaN(v) ? "—" : v.toFixed(d) + u;
    const st = H.stats[ev.conf >= 2 ? "CONF_GE2" : ev.conf === 1 ? "CONF_1" : "CONF_LE0"];
    const adv = advice(ev);
    return `<div class="kpi" style="margin-top:8px">
      <div><small>VXN</small><b>${f(r.vxn, 1)}</b></div>
      <div><small>VIX÷VIX3M</small><b>${f(ev.ts, 3)}</b><small>${ev.inv ? "倒掛 " + ev.inv + " 日" : "正常"}</small></div>
      <div><small>高收益債利差</small><b>${f(r.hy, 2, "%")}</b><small>5 日 ${ev.hy5 == null ? "—" : (ev.hy5 >= 0 ? "+" : "") + ev.hy5.toFixed(2)}</small></div>
      <div><small>10年實質利率</small><b>${f(r.rr, 2, "%")}</b></div></div>
      <div style="margin-top:10px;padding:10px 12px;border-radius:10px;background:var(--bg)">
      <div style="font-weight:700">${adv.head}</div>
      ${adv.notes.map(n => `<div class="sub">・${n}</div>`).join("")}
      ${ev.light === "GREEN" && st ? `<div class="sub">歷史同分數綠燈（${st.days} 天）：S&amp;P 1 個月 ${pct(st.spx_1m[0])}／勝率 ${(st.spx_1m[1] * 100).toFixed(0)}%，3 個月 ${pct(st.spx_3m[0])}／${(st.spx_3m[1] * 100).toFixed(0)}%</div>` : ""}
      ${(ev.light === "RED" || ev.light === "RED2") && H.stats[ev.light] ? `<div class="sub">歷史同燈號（${H.stats[ev.light].days} 天）：S&amp;P 1 個月 ${pct(H.stats[ev.light].spx_1m[0])}／${(H.stats[ev.light].spx_1m[1] * 100).toFixed(0)}%，3 個月 ${pct(H.stats[ev.light].spx_3m[0])}／${(H.stats[ev.light].spx_3m[1] * 100).toFixed(0)}%；半導體 3 個月 ${pct(H.stats[ev.light].sox_3m[0])}</div>` : ""}
      <div class="sub">${(ev.light === "RED" || ev.light === "RED2") ? "減碼步驟：紅燈減高 beta 約 1/3 → 轉深紅再減 1/3。" : "進場規則：綠燈第一天先等待 → 分數自高點回落 ≥5 為第 1 批；再度轉綠創高後回落 ≥5 為下一批（最多 3 批）；事件前一日的訊號改在事件公布當日收盤執行。機會資金 = 目前現金 − 最低現金水位。"}非投資建議。</div></div>`;
  }
  function needText(r) {
    if ((r.fg <= GREEN_FG && r.vix >= GREEN_VIX) || r.vix >= GREEN_VIX_ALONE) return "";
    if (r.fg >= 75) return "情緒過熱區：F&G ≥ 75，留意紅燈退場警示";
    const a = [`VIX 升至 ${GREEN_VIX_ALONE}（目前 ${r.vix.toFixed(1)}）`];
    a.push(`或 F&G 降至 ${GREEN_FG} 以下且 VIX ≥ ${GREEN_VIX}`);
    return "距離綠燈：需 " + a.join("，");
  }

  function renderSignal(ev, label) {
    const [txt, color] = LIGHT[ev.light], r = ev.r;
    $("fs-lamp").style.background = color; $("fs-lamp").style.boxShadow = `0 0 26px ${color}88`;
    $("fs-lamp").setAttribute("aria-label", txt);
    $("fs-light").textContent = txt;
    $("fs-detail").innerHTML = `${label}｜綜合恐慌分數 <b>${ev.score.toFixed(1)}</b> / 100｜綠燈：F&amp;G≤${GREEN_FG} 且 VIX≥${GREEN_VIX}，或 VIX≥${GREEN_VIX_ALONE}｜紅燈：F&amp;G≥75 且（VIX&lt;15 或 HY 利差一年百分位≤20）`;
    $("fs-need").textContent = needText(r);
    const ff = k => r["ff_" + k] ? "<small style='color:#c98500'>沿用前值</small>" : "";
    $("fs-kpi").innerHTML =
      `<div><small>恐懼貪婪</small><b>${r.fg.toFixed(1)}</b><small>恐慌百分位 ${ev.ffg.toFixed(0)}</small>${ff("fg")}</div>` +
      `<div><small>VIX</small><b>${r.vix.toFixed(2)}</b><small>百分位 ${ev.fvix.toFixed(0)}</small>${ff("vix")}</div>` +
      `<div><small>MOVE</small><b>${r.move.toFixed(1)}</b><small>百分位 ${ev.fmove.toFixed(0)}</small>${ff("move")}</div>` +
      `<div><small>綜合分數</small><b>${ev.score.toFixed(0)}</b><small>0 貪婪→100 恐慌</small></div>` ;
    $("fs-macro").innerHTML = macroKpi(ev);
    const st = H.stats[ev.light], base = H.stats.ALL;
    $("fs-hist").innerHTML = "<tr><th></th>" + IDX.map(x => `<th>${x[1]}</th>`).join("") + "</tr>" +
      HZ.map(([h, hz]) => `<tr><td>${hz}</td>` + IDX.map(([k]) => {
        const [m, w] = st[`${k}_${h}`], [bm] = base[`${k}_${h}`];
        return `<td><b class="${cls(m)}">${pct(m)}</b> <small>${(w * 100).toFixed(0)}%｜基準 ${pct(bm)}</small></td>`;
      }).join("") + "</tr>").join("");
  }

  function renderTracking(S) {
    const n = S.length, start = Math.max(0, n - 126), evs = [];
    for (let i = start; i < n; i++) evs.push({i, ...evaluate(S, i)});
    const greens = evs.filter(e => e.light === "GREEN"), groups = [];
    for (const g of greens) {
      const last = groups[groups.length - 1];
      if (last && g.i - last[last.length - 1].i <= 10) last.push(g); else groups.push([g]);
    }
    const L = S[n - 1];
    if (!groups.length) { $("fs-trk").innerHTML = "<tr><td>近半年沒有出現綠燈。</td></tr>"; return; }
    $("fs-trk").innerHTML = "<tr><th>首次綠燈</th><th>綠燈天數</th><th>經過<br><small>交易日</small></th>" +
      IDX.map(x => `<th>${x[1]}<br><small>一次買｜分批</small></th>`).join("") + "</tr>" +
      groups.reverse().map(g => {
        const f = S[g[0].i];
        return `<tr><td>${f.date}<br><small>至 ${S[g[g.length - 1].i].date.slice(5)}</small></td><td>${g.length}</td><td>${n - 1 - g[0].i}</td>` +
          IDX.map(([k]) => {
            const one = L[k] / f[k] - 1, cost = g.length / g.reduce((s, e) => s + 1 / S[e.i][k], 0), st = L[k] / cost - 1;
            return `<td><b class="${cls(one)}">${pct(one)}</b><br><small class="${cls(st)}">${pct(st)}</small></td>`;
          }).join("") + "</tr>";
      }).join("") +
      `<tr><td colspan="6"><small>最新收盤日 ${L.date}${L.ff_spx ? "（指數收盤沿用前值，請輸入收盤價以更新報酬）" : ""}</small></td></tr>`;
  }

  function renderEntries() {
    const list = Object.values(entries).sort((a, b) => a.date < b.date ? 1 : -1);
    $("ent-n").textContent = list.length;
    $("fs-ent").innerHTML = list.length ? "<tr><th>日期</th><th>F&amp;G</th><th>VIX</th><th>MOVE</th><th>S&amp;P</th><th>納指</th><th>半導體</th><th>VXN</th><th>VIX3M</th><th>HY</th><th>實質利率</th><th></th></tr>" +
      list.map(e => `<tr><td>${e.date}</td>` + ["fg", "vix", "move", "spx", "ndx", "sox", "vxn", "vix3m", "hy", "rr"].map(k => `<td>${e[k] ?? "—"}</td>`).join("") +
        `<td><button data-del="${e.date}">刪除</button></td></tr>`).join("") : "<tr><td>尚無手動更新。</td></tr>";
  }

  function refresh() {
    const S0 = series(); PLAN = computePlan(S0);
    const S = S0, ev = evaluate(S, S.length - 1);
    const last = S[S.length - 1];
    $("fs-asof").textContent = `最新資料日 ${last.date}${last.manual ? "（手動更新）" : ""}｜自動資料截至 ${H.db_date}`;
    renderSignal(ev, `資料日 ${last.date}`);
    renderTracking(S); renderEntries();
    const up = upcoming(S[S.length - 1].date, 45);
    $("fs-events").innerHTML = up.length ? up.map(e => `<tr><td>${e.date.slice(5)}</td><td>${e.importance === "高" ? "🔴" : "⚪"} ${e.event}</td>` +
      `<td><small>${e.importance === "高" ? "前一日的進場訊號改在當日收盤執行" : ""}</small></td></tr>`).join("") : "<tr><td>未來 45 天沒有登錄的重大事件</td></tr>";
    const d = new Date(last.date + "T00:00:00"); do { d.setDate(d.getDate() + 1); } while (d.getDay() === 0 || d.getDay() === 6);
    const ymd = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    if (!$("in-date").value) $("in-date").value = ymd;
    if (!$("q-date").value) { $("q-date").value = last.date; query(); }
    $("q-date").max = ymd;
    const dd = H.data_dates || {};
    $("q-fresh").innerHTML = `資料更新於：${H.updated_at || H.db_date}｜各指標最新日：` +
      ["fg", "vix", "move", "spx", "ndx", "sox"].filter(k => dd[k]).map(k => `${k.toUpperCase()} ${dd[k].slice(5)}`).join("、");
  }

  function nearestIdx(S, date) {
    let i = -1; for (let k = 0; k < S.length; k++) { if (S[k].date <= date) i = k; else break; } return i;
  }
  function query() {
    const date = $("q-date").value, S = series(), last = S[S.length - 1];
    if (!date) { $("q-msg").textContent = "請選擇日期"; return; }
    if (date > last.date) {
      $("q-msg").innerHTML = `資料目前只到 <b>${last.date}</b>（控制台每個交易日 08:07 自動更新，下一次更新後即可查詢）。若要先看 ${date}，請在下方手動輸入。`;
      $("q-out").innerHTML = ""; $("man-box").open = true; $("in-date").value = date; return;
    }
    const i = nearestIdx(S, date);
    if (i < WIN * 0.8) { $("q-msg").textContent = "此日期太早，資料不足以計算百分位。"; $("q-out").innerHTML = ""; return; }
    const ev = evaluate(S, i), r = ev.r, [txt, color] = LIGHT[ev.light];
    $("q-msg").textContent = r.date !== date ? `${date} 非交易日，改用前一個交易日 ${r.date}。` : "";
    const ff = k => r["ff_" + k] ? " <small style='color:#c98500'>(沿用前值)</small>" : "";
    const fwd = (k, n) => i + n < S.length ? S[i + n][k] / r[k] - 1 : null;
    const rows = [["2週", 10, 0], ["1個月", 21, 0], ["3個月", 63, 0], ["至最新", S.length - 1 - i, 1]].map(([lab, n, isLast]) =>
      `<tr><td>${lab}${isLast ? `<br><small>${last.date}</small>` : ""}</td>` +
      IDX.map(([k]) => { const v = n > 0 ? fwd(k, n) : null;
        return `<td class="${cls(v)}">${v == null ? (isLast ? "—" : "未滿期") : pct(v)}</td>`; }).join("") + "</tr>").join("");
    $("q-out").innerHTML = `
      <div class="light" style="margin-top:10px"><div class="fs-lamp" style="width:44px;height:44px;background:${color}"></div><div>
        <div class="big" style="font-size:18px">${r.date}：${txt}</div>
        <div class="sub">綜合恐慌分數 <b>${ev.score.toFixed(1)}</b>｜${r.manual ? "手動輸入" : "自動資料"}</div></div></div>
      <div class="kpi">
        <div><small>恐懼貪婪</small><b>${r.fg.toFixed(1)}</b><small>恐慌百分位 ${ev.ffg.toFixed(0)}</small>${ff("fg")}</div>
        <div><small>VIX</small><b>${r.vix.toFixed(2)}</b><small>百分位 ${ev.fvix.toFixed(0)}</small>${ff("vix")}</div>
        <div><small>MOVE</small><b>${r.move.toFixed(1)}</b><small>百分位 ${ev.fmove.toFixed(0)}</small>${ff("move")}</div>
        <div><small>綜合分數</small><b>${ev.score.toFixed(0)}</b><small>${needText(r) ? "未達綠燈" : "符合綠燈"}</small></div></div>
      ${macroKpi(ev)}
      <div class="sub" style="margin-top:8px">進場規則狀態：${ev.phase === "BUY" ? `<b>第 ${ev.tranche} 批進場訊號</b>` : ev.phase === "WAIT" ? "等待分數轉折" : ev.phase === "HOLD" ? `已發出 ${ev.done} 批，等待再轉綠` : "—"}</div>
      <div class="sub" style="margin-top:8px">當日收盤買進後的實際表現：</div>
      <div class="tw"><table class="mini"><tr><th>持有</th>${IDX.map(x => `<th>${x[1]}</th>`).join("")}</tr>${rows}</table></div>
      <div class="sub" style="margin-top:6px">歷史同燈號平均（1個月）：S&amp;P ${pct(H.stats[ev.light].spx_1m[0])}、勝率 ${(H.stats[ev.light].spx_1m[1] * 100).toFixed(0)}%</div>`;
  }

  function readForm() {
    const e = {date: $("in-date").value, fg: num("in-fg"), vix: num("in-vix"), move: num("in-move"),
               spx: num("in-spx"), ndx: num("in-ndx"), sox: num("in-sox"),
               vxn: num("in-vxn"), vix3m: num("in-vix3m"), hy: num("in-hy"), rr: num("in-rr")};
    if (!e.date) throw "請填日期";
    if (e.fg == null || e.fg < 0 || e.fg > 100) throw "請填 0–100 的恐懼貪婪指數";
    if (e.vix == null || e.vix <= 0) throw "請填 VIX";
    return e;
  }
  function trial() {
    try {
      const e = readForm(), saved = entries; entries = {...entries, [e.date]: e};
      const S = series(), keep = PLAN; PLAN = computePlan(S);
      const i = S.findIndex(r => r.date === e.date), ev = evaluate(S, i);
      entries = saved;
      renderSignal(ev, `試算 ${e.date}（尚未儲存）`); PLAN = keep;
      $("fs-msg").textContent = "已試算。按「計算並儲存」才會保留這筆數據。";
    } catch (err) { $("fs-msg").textContent = String(err); }
  }
  async function save() {
    let e; try { e = readForm(); } catch (err) { $("fs-msg").textContent = String(err); return; }
    if (!db) { entries[e.date] = e; refresh(); $("fs-msg").textContent = "已計算（本次檢視有效；此檢視無法永久儲存）。"; $("q-date").value = e.date; query(); return; }
    $("btn-save").disabled = true; $("fs-msg").textContent = "儲存中…";
    try {
      const clean = Object.fromEntries(Object.entries(e).filter(([, v]) => v != null));
      await db.collection("entries").doc(e.date).set({...clean, saved_at: new Date().toISOString()});
      $("fs-msg").textContent = `已儲存 ${e.date}。`; $("q-date").value = e.date; setTimeout(query, 300);
      ["in-fg", "in-vix", "in-move", "in-spx", "in-ndx", "in-sox", "in-vxn", "in-vix3m", "in-hy", "in-rr"].forEach(id => $(id).value = "");
      $("in-date").value = "";
    } catch (err) { $("fs-msg").textContent = "儲存失敗：" + (err.message || err.code || err); }
    $("btn-save").disabled = false;
  }
  async function del(date) {
    if (!db) { delete entries[date]; refresh(); return; }
    try { await db.collection("entries").doc(date).delete(); } catch (err) { $("fs-msg").textContent = "刪除失敗：" + (err.message || err); }
  }

  async function dl(path, filename) {
    const msg = $("dl-msg");
    try {
      const downloads = await claude.use("downloads");
      if (!downloads) { msg.textContent = "此檢視無法下載，請在 claude.ai 開啟此頁面。"; return; }
      msg.textContent = "準備檔案中…";
      const blob = path instanceof Blob ? path : await (await fetch(path)).blob();
      await downloads.save({filename, data: blob});
      msg.textContent = "已下載 " + filename;
    } catch (err) { msg.textContent = err.code === "declined" ? "已取消下載。" : "下載失敗：" + (err.message || err.code || err); }
  }
  function csv() {
    const S = series();
    const lines = ["date,fg,vix,move,spx,ndx,sox,manual"].concat(
      S.map(r => [r.date, r.fg, r.vix, r.move, r.spx, r.ndx, r.sox, r.manual ? 1 : 0].join(",")));
    dl(new Blob([lines.join("\n")], {type: "text/csv"}), "fear_signal_daily.csv");
  }

  $("btn-q").onclick = query; $("q-date").onchange = query;
  $("btn-calc").onclick = trial; $("btn-save").onclick = save;
  $("fs-ent").onclick = ev => { const d = ev.target.dataset && ev.target.dataset.del; if (d) del(d); };
  $("btn-zip").onclick = async () => {
    $("dl-msg").textContent = "打包中…";
    try {
      if (!window.JSZip) await new Promise((ok, no) => { const s = document.createElement("script");
        s.src = "https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"; s.onload = ok; s.onerror = no; document.head.appendChild(s); });
      const man = await (await fetch("pkg/manifest.json")).json(), zip = new JSZip(), root = zip.folder("fear-signal");
      for (const [file, path] of Object.entries(man)) root.file(path, await (await fetch("pkg/" + file)).text());
      root.file("reports/fear_signal_10y_report.pdf", await (await fetch("files/report.pdf")).arrayBuffer());
      for (const f of ["daily_report.pdf", "score_charts.pdf", "score_overview.png", "ytd_2025.pdf", "ytd_2026.pdf"])
        root.file("docs/" + f, await (await fetch("files/" + f)).arrayBuffer());
      root.file("docs/.keep", "");
      dl(await zip.generateAsync({type: "blob"}), "fear-signal.zip");
    } catch (err) { $("dl-msg").textContent = "打包失敗：" + (err.message || err); }
  };
  $("btn-daily").onclick = () => dl("files/daily_report.pdf", `當日報告_${$("btn-daily").dataset.date}.pdf`);
  $("btn-pdf").onclick = () => dl("files/report.pdf", "恐慌情緒10年資料庫報告.pdf");
  $("btn-csv").onclick = csv;
  $("btn-year").onclick = () => { const y = $("sel-year").value; dl(`files/yearly_${y}.pdf`, `${y}年度燈號回測報告.pdf`); };
  $("btn-charts").onclick = () => dl("files/score_charts.pdf", "綜合分數vs三大指數_2025-2026.pdf");
  $("btn-ytd26").onclick = () => dl("files/ytd_2026.pdf", "2026年每日燈號回測.pdf");
  $("btn-ytd25").onclick = () => dl("files/ytd_2025.pdf", "2025年每日燈號回測.pdf");

  fetch("history.json").then(r => r.json()).then(j => { H = j; refresh(); connectDb(); })
    .catch(err => { console.error(err); $("fs-asof").textContent = "資料載入失敗，請重新整理。"; });

  async function connectDb() {
    try {
      if (!window.claude) return;
      const timer = setTimeout(() => { if (!db) $("fs-msg").textContent += "（雲端儲存未連線）"; }, 10000);
      db = await claude.use("db");
      clearTimeout(timer);
      if (!db) return;
      db.collection("entries").onSnapshot(snap => {
        const m = {}; (snap.docs || snap).forEach(d => { const v = d.data ? d.data() : d; if (v && v.date) m[v.date] = v; });
        entries = m; refresh();
      }, err => { $("fs-msg").textContent = "讀取儲存資料失敗：" + (err.message || err); });
    } catch (e) { db = null; }
  }
})();
</script>
