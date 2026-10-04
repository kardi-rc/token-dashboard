import { fmt, $ } from '/web/app.js';
import { donutChart, stackedBarChart } from '/web/charts.js';

const esc = fmt.htmlSafe;

export const PROVIDER_PREFIXES = [
  ['claude', 'anthropic'], ['gemini', 'google'], ['qwen', 'alibaba'], ['deepseek', 'deepseek'],
  ['gpt', 'openai'], ['o3', 'openai'], ['o1', 'openai'], ['glm', 'zhipu'], ['kimi', 'moonshot'],
];

// Normalizes slash-derived namespace prefixes so 'vendor/model' lands in the
// same bucket as the bare model name (e.g. 'deepseek-ai/deepseek-v3' → 'deepseek').
// Unknown namespaces map to themselves (identity default).
export const PROVIDER_ALIASES = { 'deepseek-ai': 'deepseek', 'moonshotai': 'moonshot' };

export function deriveProvider(model) {
  if (model === 'unknown' || !model || typeof model !== 'string') return 'unknown';
  if (model.includes('/')) { const ns = model.split('/')[0]; return PROVIDER_ALIASES[ns] || ns; }
  for (const [pfx, prov] of PROVIDER_PREFIXES) {
    if (model.startsWith(pfx)) return prov;
  }
  return 'other';
}

export function buildParams(period, customStart, customEnd) {
  if (period === '7d' || period === '30d' || period === '90d') {
    return { since: new Date(Date.now() - parseInt(period, 10) * 864e5).toISOString(), until: null };
  }
  if (period === 'custom' && customStart && customEnd) {
    const s = new Date(customStart + 'T00:00:00'), u = new Date(new Date(customEnd + 'T00:00:00').getTime() + 864e5);
    return { since: isNaN(s) ? customStart : s.toISOString(), until: isNaN(u) ? customEnd : u.toISOString() };
  }
  return { since: null, until: null };
}

async function fetchCostSeries(params) {
  const p = new URLSearchParams();
  if (params.since) p.set('since', params.since);
  if (params.until) p.set('until', params.until);
  const r = await fetch('/api/cost-series' + (p.toString() ? '?' + p : ''));
  if (!r.ok) { const e = new Error((await r.text()) || `HTTP ${r.status}`); e.status = r.status; throw e; }
  return r.json();
}

function aggregateByProvider(rows, period, customStart, customEnd) {
  const map = new Map();
  let totalCost = 0, minDate = null, maxDate = null;
  for (const r of rows) {
    const prov = deriveProvider(r.model);
    if (!map.has(prov)) map.set(prov, { provider: prov, cost: 0, turns: 0 });
    const p = map.get(prov);
    p.turns += r.turns || 0;
    if (r.cost_usd != null) { p.cost += r.cost_usd; totalCost += r.cost_usd; }
    if (r.date) { if (!minDate || r.date < minDate) minDate = r.date; if (!maxDate || r.date > maxDate) maxDate = r.date; }
  }
  let divisor = 30;
  if (period === '7d') divisor = 7;
  else if (period === '90d') divisor = 90;
  else if (period === 'custom' && customStart && customEnd) {
    divisor = Math.max(1, Math.round((new Date(customEnd + 'T00:00:00') - new Date(customStart + 'T00:00:00')) / 864e5) + 1);
  } else if (period === 'all' && minDate && maxDate) {
    divisor = Math.max(1, Math.round((new Date(maxDate + 'T00:00:00') - new Date(minDate + 'T00:00:00')) / 864e5) + 1);
  }
  const providers = Array.from(map.values()).map(p => ({
    ...p, pct: totalCost > 0 ? p.cost / totalCost : 0, monthlyExtrap: (p.cost / divisor) * 30,
  })).sort((a, b) => b.cost - a.cost || a.provider.localeCompare(b.provider));
  return { providers, totalCost };
}

function aggregateByDate(rows, providers) {
  const categories = Array.from(new Set(rows.map(r => r.date).filter(Boolean))).sort();
  const costMap = new Map();
  for (const r of rows) {
    if (r.cost_usd != null && r.date) {
      const k = r.date + '|' + deriveProvider(r.model);
      costMap.set(k, (costMap.get(k) || 0) + r.cost_usd);
    }
  }
  // Chart series names are rendered as HTML by the ECharts tooltip formatter
  // (innerHTML) — provider strings are derived from DB model text, escape them.
  // The costMap keys below stay raw: they are computation-only, never emitted.
  const series = providers.map(p => ({
    name: esc(p.provider),
    values: categories.map(d => Number((costMap.get(d + '|' + p.provider) || 0).toFixed(4))),
  }));
  return { categories, series };
}

function aggregateByModel(rows) {
  const map = new Map();
  let totalCost = 0, totalTurns = 0, totalIn = 0, totalOut = 0, totalCache = 0, unpricedCount = 0;
  for (const r of rows) {
    const m = r.model || 'unknown';
    if (!map.has(m)) map.set(m, { model: m, provider: deriveProvider(m), turns: 0, input: 0, output: 0, cache: 0, cost: null, hasCost: false, cost_estimated: false });
    const e = map.get(m);
    e.turns += r.turns || 0; e.input += r.input_tokens || 0; e.output += r.output_tokens || 0;
    e.cache += (r.cache_read_tokens || 0) + (r.cache_create_5m_tokens || 0) + (r.cache_create_1h_tokens || 0);
    if (r.cost_usd != null) { e.cost = (e.cost || 0) + r.cost_usd; e.hasCost = true; if (r.cost_estimated) e.cost_estimated = true; }
  }
  const models = Array.from(map.values()).map(m => {
    totalTurns += m.turns; totalIn += m.input; totalOut += m.output; totalCache += m.cache;
    if (m.hasCost) totalCost += m.cost; else unpricedCount++;
    return m;
  });
  for (const m of models) m.pct = (m.hasCost && totalCost > 0) ? m.cost / totalCost : (m.hasCost ? 0 : null);
  return { models, totalCost, totalTurns, totalIn, totalOut, totalCache, unpricedCount };
}

export default async function (root) {
  let curPeriod = '30d', curStart = '', curEnd = '', curModelData = null;
  let sortCol = 'cost', sortAsc = false;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px;flex-wrap:wrap">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Costs</h2>
      <span class="muted" id="costs-sub" style="font-size:12px">last 30 days</span>
      <div class="spacer"></div>
      <div class="range-tabs" role="tablist">
        ${['7d', '30d', '90d', 'all'].map(k => `<button data-range="${k}" class="${k==='30d'?'active':''}">${k==='all'?'All':k}</button>`).join('')}
      </div>
      <div class="flex" style="margin-left:8px">
        <input type="date" id="cost-since" style="background:var(--panel-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:3px 6px;font-family:var(--sans);font-size:12px;color-scheme:dark">
        <span class="muted" style="font-size:11px">to</span>
        <input type="date" id="cost-until" style="background:var(--panel-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:3px 6px;font-family:var(--sans);font-size:12px;color-scheme:dark">
        <button id="cost-apply" style="padding:4px 8px;font-size:12px">Apply</button>
      </div>
    </div>
    <div id="cost-err" class="muted" style="color:var(--bad);font-size:12px;margin:-8px 0 12px;text-align:right;display:none"></div>
    <div id="costs-empty" class="card" style="display:none;padding:24px;text-align:center"><p class="muted" style="margin:0">No cost data in this period</p></div>
    <div id="costs-content">
      <div id="costs-kpis" class="row" style="grid-template-columns:repeat(auto-fill, minmax(200px, 1fr))"></div>
      <div class="row cols-2" style="margin-top:16px">
        <div class="card"><h3>Cost share by provider</h3><div id="ch-donut-wrap"><div id="ch-provider-donut" style="height:300px"></div></div></div>
        <div class="card"><h3>Daily cost by provider</h3><div id="ch-bar-wrap"><div id="ch-daily-cost" style="height:300px"></div></div></div>
      </div>
      <div id="costs-table-section"></div>
    </div>`;

  const errEl = $('#cost-err', root), subEl = $('#costs-sub', root);
  const emptyEl = $('#costs-empty', root), contentEl = $('#costs-content', root);
  const kpisEl = $('#costs-kpis', root), donutWrap = $('#ch-donut-wrap', root);
  const barWrap = $('#ch-bar-wrap', root), tableEl = $('#costs-table-section', root);

  function renderTable() {
    if (!curModelData) return;
    const { models, totalCost, totalTurns, totalIn, totalOut, totalCache, unpricedCount } = curModelData;
    models.sort((a, b) => {
      if (sortCol === 'cost' || sortCol === 'pct') {
        if (!a.hasCost && !b.hasCost) return a.model.localeCompare(b.model);
        if (!a.hasCost) return 1; if (!b.hasCost) return -1;
        const d = a.cost - b.cost || a.model.localeCompare(b.model);
        return sortAsc ? d : -d;
      }
      const v = (sortCol === 'model' || sortCol === 'provider') ? a[sortCol].localeCompare(b[sortCol]) : (a[sortCol] || 0) - (b[sortCol] || 0);
      return (sortAsc ? v : -v) || a.model.localeCompare(b.model);
    });
    const arrow = c => sortCol === c ? (sortAsc ? ' ↑' : ' ↓') : '';
    const cols = [['model','model',0],['provider','provider',0],['turns','turns',1],['input','input tokens',1],['output','output tokens',1],['cache','cache tokens',1],['cost','cost USD',1],['pct','% of total',1]];
    tableEl.innerHTML = `
      <div class="card" style="margin-top:16px">
        <h3>Cost by model</h3>
        <table>
          <thead><tr>${cols.map(([k,l,n]) => `<th data-col="${k}" class="${n?'num':''}" style="cursor:pointer;user-select:none">${l}${arrow(k)}</th>`).join('')}</tr></thead>
          <tbody>
            ${models.map(m => `<tr>
              <td>${esc(m.model)}</td><td>${esc(m.provider)}</td>
              <td class="num">${fmt.int(m.turns)}</td><td class="num">${fmt.int(m.input)}</td>
              <td class="num">${fmt.int(m.output)}</td><td class="num">${fmt.int(m.cache)}</td>
              <td class="num">${m.hasCost ? fmt.usd(m.cost) + (m.cost_estimated ? ' <span class="badge">est.</span>' : '') : '—'}</td>
              <td class="num">${m.hasCost ? fmt.pct(m.pct) : '—'}</td>
            </tr>`).join('')}
          </tbody>
          <tfoot><tr style="font-weight:600">
            <td>Total</td><td></td><td class="num">${fmt.int(totalTurns)}</td><td class="num">${fmt.int(totalIn)}</td>
            <td class="num">${fmt.int(totalOut)}</td><td class="num">${fmt.int(totalCache)}</td><td class="num">${fmt.usd(totalCost)}</td><td class="num">100%</td>
          </tr></tfoot>
        </table>
        ${unpricedCount > 0 ? `<p class="muted" style="margin-top:8px;font-size:11px">${unpricedCount} model${unpricedCount > 1 ? 's' : ''} without rate — excluded from cost totals</p>` : ''}
      </div>`;
    tableEl.querySelectorAll('th[data-col]').forEach(th => th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (sortCol === col) sortAsc = !sortAsc;
      else { sortCol = col; sortAsc = (col === 'model' || col === 'provider'); }
      renderTable();
    }));
  }

  function renderData(rows) {
    if (!rows.length) { emptyEl.style.display = 'block'; contentEl.style.display = 'none'; return; }
    emptyEl.style.display = 'none'; contentEl.style.display = 'block';
    const { providers } = aggregateByProvider(rows, curPeriod, curStart, curEnd);
    kpisEl.innerHTML = providers.map(p => `
      <div class="card kpi cost"><div class="label">${esc(p.provider)}</div>
        <div class="value" title="${fmt.usd(p.cost)}">${fmt.usd(p.cost)}</div>
        <div class="sub">${fmt.pct(p.pct)} of total · ${fmt.usd(p.monthlyExtrap)}/mo est.</div></div>`).join('');
    const donutData = providers.map(p => ({ name: esc(p.provider), value: Number(p.cost.toFixed(4)) })).filter(d => d.value > 0);
    donutWrap.innerHTML = '<div id="ch-provider-donut" style="height:300px"></div>';
    donutChart($('#ch-provider-donut', donutWrap), donutData, { unit: 'usd' });
    const barData = aggregateByDate(rows, providers);
    barWrap.innerHTML = '<div id="ch-daily-cost" style="height:300px"></div>';
    stackedBarChart($('#ch-daily-cost', barWrap), { categories: barData.categories, series: barData.series, formatter: v => fmt.usd(v) });
    curModelData = aggregateByModel(rows);
    renderTable();
  }

  async function load(period, s, u) {
    try {
      const data = await fetchCostSeries(buildParams(period, s, u));
      errEl.style.display = 'none'; curPeriod = period; curStart = s || ''; curEnd = u || '';
      subEl.textContent = period === 'all' ? 'all time' : (period === 'custom' ? `${s} to ${u}` : `last ${parseInt(period, 10)} days`);
      root.querySelectorAll('.range-tabs button').forEach(b => b.classList.toggle('active', b.dataset.range === curPeriod));
      renderData(data.rows || []);
    } catch (e) {
      errEl.textContent = e.message || 'Error loading cost data'; errEl.style.display = 'block';
    }
  }

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => { $('#cost-since', root).value = ''; $('#cost-until', root).value = ''; load(btn.dataset.range); });
  });
  $('#cost-apply', root).addEventListener('click', () => {
    const s = $('#cost-since', root).value, u = $('#cost-until', root).value;
    if (!s || !u) { errEl.textContent = 'Please select both start and end dates.'; errEl.style.display = 'block'; return; }
    load('custom', s, u);
  });
  await load(curPeriod);
}
