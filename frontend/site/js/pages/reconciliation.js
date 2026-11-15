/**
 * 回传对账：实验平台（分流）与指标库（转化）每天各回传一次。
 * 页面把「齐全 / 延迟 / 冲突 / 已和解」分开处理；同批统计日齐全后才允许生成分析批次。
 */
import { api } from '../api.js';
import { ENUMS, analysisTone, can, reconcileTone } from '../config.js';
import {
  badge,
  drawer,
  errorBox,
  errorMessage,
  esc,
  field,
  filterBar,
  fmtP,
  fmtTime,
  kv,
  num,
  pct,
  statStrip,
  table,
  toast,
} from '../ui.js';

const state = { experimentId: '', status: 'all', dateFrom: '', dateTo: '' };

async function loadExperiments() {
  const data = await api.get('/api/experiments', { limit: 50 });
  return data.items;
}

function pickExperiment(items, preferred) {
  if (preferred && items.some((item) => String(item.id) === String(preferred))) {
    return items.find((item) => String(item.id) === String(preferred));
  }
  if (state.experimentId && items.some((item) => String(item.id) === String(state.experimentId))) {
    return items.find((item) => String(item.id) === String(state.experimentId));
  }
  const withReturns = items.find((item) => item.srm && item.srm.payload && item.srm.payload.cells);
  return withReturns || items[0];
}

function batchCell(batch) {
  if (!batch) return `<span class="badge pending">未回传</span>`;
  const superseded = batch.status === 'superseded';
  return `<span class="mono">${esc(batch.batch_no)}</span> <span class="muted">rev.${esc(batch.revision)}</span>
    <div class="tiny muted">${num(batch.rows_count)} 行 · ${fmtTime(batch.received_at)}</div>
    ${superseded ? '<span class="badge pending">已取代</span>' : ''}`;
}

function sourcePanel(summary) {
  const per = summary.per_source || {};
  const left = per.assignment || {};
  const right = per.metric || {};
  return `<div class="grid-2">
    <section class="panel">
      <div class="panel-head"><h2>实验平台 · 分流数</h2><span class="meta">assignment source</span></div>
      <div class="panel-body">
        ${kv([
          ['有效批次', `${num(left.active_batches || 0)} 个 · 覆盖 ${num(left.active_dates || 0)} 天`],
          ['明细行', num(left.rows || 0)],
          ['分流数合计', num(left.samples_total || 0)],
          ['最近回传', fmtTime(left.latest_received_at)],
        ])}
        ${summary.missing_assignment_dates && summary.missing_assignment_dates.length
          ? `<p class="muted" style="margin:6px 0 0">缺数据日：${summary.missing_assignment_dates.map((day) => esc(day)).join('、')}</p>`
          : ''}
      </div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>指标库 · 转化数</h2><span class="meta">metric source</span></div>
      <div class="panel-body">
        ${kv([
          ['有效批次', `${num(right.active_batches || 0)} 个 · 覆盖 ${num(right.active_dates || 0)} 天`],
          ['明细行', num(right.rows || 0)],
          ['转化数合计', num(right.conversions_total || 0)],
          ['最近回传', fmtTime(right.latest_received_at)],
        ])}
        ${summary.missing_metric_dates && summary.missing_metric_dates.length
          ? `<p class="muted" style="margin:6px 0 0">缺数据日：${summary.missing_metric_dates.map((day) => esc(day)).join('、')}</p>`
          : ''}
      </div>
    </section>
  </div>`;
}

function dayTable(summary) {
  return table({
    rows: (summary.days || []).map((day) => ({ ...day, id: day.stat_date })),
    columns: [
      { key: 'stat_date', label: '统计日', className: 'mono nowrap' },
      {
        key: 'status',
        label: '对账状态',
        render: (row) => badge(reconcileTone(row.status), ENUMS.reconcile[row.status] || row.status),
      },
      { key: 'assignment_batch', label: '实验平台批次', render: (row) => batchCell(row.assignment_batch) },
      { key: 'metric_batch', label: '指标库批次', render: (row) => batchCell(row.metric_batch) },
      {
        key: 'counts',
        label: '格数（齐全/和解/延迟/冲突）',
        className: 'num',
        render: (row) => `${num(row.complete)} / ${num(row.reconciled)} / ${num(row.delayed)} / ${num(row.conflict)}`,
      },
      {
        key: 'ready',
        label: '可参与分析',
        render: (row) => (row.ready ? badge('ok', '是') : badge('pending', '否')),
      },
    ],
    empty: { title: '还没有回传批次', hint: '先接入实验平台与指标库的每日回传。' },
  });
}

function blockingTable(items, ctx) {
  if (!items || !items.length) {
    return `<div class="empty"><div class="mark">全部齐全</div><p>两份回传逐格一致，可以生成分析批次。</p></div>`;
  }
  return table({
    rows: items.map((item) => ({ ...item, id: item.id || `${item.stat_date}-${item.variant_code}-${item.channel}-${item.metric_key}` })),
    columns: [
      { key: 'stat_date', label: '统计日', className: 'mono' },
      { key: 'variant_code', label: '变体', className: 'mono' },
      { key: 'channel_label', label: '渠道' },
      { key: 'metric_label', label: '指标' },
      {
        key: 'status',
        label: '状态',
        render: (row) => badge(reconcileTone(row.status), ENUMS.reconcile[row.status] || row.status),
      },
      {
        key: 'reason',
        label: '原因',
        render: (row) => esc(row.reason_label || ENUMS.reconcileReason[row.reason] || row.reason || ''),
      },
      {
        key: 'sides',
        label: '两侧数值',
        render: (row) => `实验平台 ${row.assignment_samples === null ? '—' : num(row.assignment_samples)} / 指标库 ${row.metric_samples === null ? '—' : num(row.metric_samples)}`,
      },
      {
        key: 'action',
        label: '处理',
        render: (row) =>
          row.status === 'conflict'
            ? can(ctx.user.role, 'srm:run')
              ? `<button class="btn sm ghost" data-explain="${esc(row.id)}">写解释</button>`
              : '<span class="mono muted">需实验负责人</span>'
            : row.status === 'delayed'
              ? '<span class="mono muted">等另一侧回传</span>'
              : '',
      },
    ],
    empty: { title: '没有待处理格', hint: '' },
  });
}

function runPanel(experiment, latest) {
  const run = latest && latest.item;
  if (!run) return '';
  const basis = run.decision_basis || {};
  const srm = run.srm || {};
  return `<section class="panel">
    <div class="panel-head">
      <h2>当前分析批次 #${esc(run.run_no)}</h2>
      <span class="meta">${badge(analysisTone(run.status), ENUMS.analysis[run.status] || run.status)} · ${fmtTime(run.computed_at)}</span>
    </div>
    <div class="panel-body" style="display:grid;gap:10px">
      ${run.status === 'invalidated' ? `<div class="error-box"><span>这一批结论已作废：${esc(run.invalidated_reason || '')}</span></div>` : ''}
      <div class="checkline">
        <div class="item"><span class="k">SRM</span><span class="v ${srm.alarm ? 'fail' : 'pass'}">χ²=${Number(srm.chi2 || 0).toFixed(2)}，p=${fmtP(srm.p_value)}</span></div>
        <div class="item"><span class="k">主指标 q</span><span class="v ${basis.significant ? 'pass' : 'fail'}">${fmtP(basis.q_value)}</span></div>
        <div class="item"><span class="k">lift</span><span class="v">${basis.lift === undefined ? '—' : pct(basis.lift, 2)}</span></div>
        <div class="item"><span class="k">建议</span><span class="v">${esc(ENUMS.verdict[basis.suggested_verdict] || basis.suggested_verdict || '—')}</span></div>
      </div>
      <p class="muted" style="margin:0">${esc(basis.rationale || '')}</p>
      <div class="panel-foot" style="border-top:1px dashed var(--rule)">
        <span class="mono muted">统计日 ${esc(run.date_from)} ~ ${esc(run.date_to)}（${run.dates_used.length} 天）</span>
        <span class="mono muted">实验平台 ${num(run.sources.assignment ? run.sources.assignment.batches : 0)} 批 · 指标库 ${num(run.sources.metric ? run.sources.metric.batches : 0)} 批</span>
        <span class="mono muted">重算 ${run.recomputed_dates.length} 天 / 沿用 ${run.reused_dates.length} 天</span>
      </div>
      ${basis.blockers && basis.blockers.length
        ? `<p class="muted" style="margin:0">阻断项：${basis.blockers.map((item) => esc(item)).join('、')}</p>`
        : ''}
    </div>
  </section>`;
}

function openIngest(view, ctx, experiments, experiment, summary) {
  const days = (summary.days || []).map((day) => day.stat_date).filter(Boolean);
  const defaultDate = days[days.length - 1] || new Date().toISOString().slice(0, 10);
  const { root, close } = drawer({
    title: '接入一次回传',
    subtitle: `${experiment.code} · 每天每条源各一次，同批次号重试不重复累计`,
    body: `<form id="ingest-form" style="display:grid;gap:12px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        ${field({
          label: '实验',
          name: 'experiment_id',
          value: experiment.id,
          options: experiments.map((item) => ({ value: item.id, label: `${item.code} · ${item.name}` })),
        })}
        ${field({
          label: '来源',
          name: 'source',
          value: 'assignment',
          options: Object.entries(ENUMS.reconcileSource).map(([value, label]) => ({ value, label })),
        })}
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        ${field({ label: '统计日', name: 'stat_date', type: 'date', value: defaultDate })}
        ${field({ label: '批次号', name: 'batch_no', placeholder: '例如 EXP-2610-metric-20260601' })}
      </div>
      ${field({
        label: '回传明细（每行：变体,渠道,指标,分流数,转化数；逗号或制表符分隔）',
        name: 'rows',
        type: 'textarea',
        grow: true,
        placeholder: 'control,feed,order_rate,1200,121',
        attrs: 'rows="7"',
      })}
      <div class="note">实验平台只报分流数、指标库只报转化数；对账按格核对，任一侧缺失算延迟，数值不同算冲突。</div>
      <div class="form-error" id="ingest-error"></div>
      <button class="btn accent" type="submit">写入回传</button>
    </form>`,
  });
  root.querySelector('#ingest-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const errorNode = root.querySelector('#ingest-error');
    const parsed = form.rows.value
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => line.split(/[,，\t]+/).map((part) => part.trim()));
    const rows = [];
    for (const parts of parsed) {
      if (parts.length !== 5) {
        errorNode.textContent = '每行需要 5 个字段：变体,渠道,指标,分流数,转化数';
        return;
      }
      rows.push({
        variant_code: parts[0],
        channel: parts[1],
        metric_key: parts[2],
        samples: Number(parts[3]),
        conversions: Number(parts[4]),
      });
    }
    if (!rows.length) {
      errorNode.textContent = '至少填一行回传明细';
      return;
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    button.textContent = '写入中…';
    try {
      const result = await api.post(`/api/experiments/${form.experiment_id.value}/return-batches`, {
        source: form.source.value,
        stat_date: form.stat_date.value,
        batch_no: form.batch_no.value.trim() || `${form.source.value}-${form.stat_date.value}`,
        rows,
      });
      const tone = result.result === 'duplicate' ? '' : result.result === 'revision' ? 'err' : '';
      toast(
        result.result === 'duplicate'
          ? '同批次同内容，已判定重试：不新增、不重复累计'
          : result.result === 'revision'
            ? `同批次号内容变了：revision ${result.batch.revision}，旧批次已取代，相关结论作废`
            : '回传已写入',
        tone,
      );
      close();
      await reconciliationPage.render(view, ctx);
    } catch (err) {
      errorNode.textContent = errorMessage(err);
      button.disabled = false;
      button.textContent = '写入回传';
    }
  });
}

function openExplain(view, ctx, cellId, experiment) {
  const { root, close } = drawer({
    title: '解释冲突格',
    subtitle: '差异必须写清原因，并指定采用哪一侧',
    body: `<form id="explain-form" style="display:grid;gap:12px">
      ${field({
        label: '采用侧',
        name: 'resolution',
        value: 'metric',
        options: [
          { value: 'assignment', label: '采用实验平台（分流）' },
          { value: 'metric', label: '采用指标库（转化）' },
        ],
      })}
      ${field({
        label: '差异原因',
        name: 'explanation',
        placeholder: '指标库把重复上报的订单去重，转化数少了 3；以指标库为准。',
      })}
      <div class="form-error" id="explain-error"></div>
      <button class="btn accent" type="submit">保存解释</button>
    </form>`,
  });
  root.querySelector('#explain-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const errorNode = root.querySelector('#explain-error');
    try {
      await api.post(`/api/experiments/${experiment.id}/reconciliation/cells/${cellId}/explain`, {
        explanation: form.explanation.value.trim(),
        resolution: form.resolution.value,
      });
      toast('冲突已和解，旧分析批次作废');
      close();
      await reconciliationPage.render(view, ctx);
    } catch (err) {
      errorNode.textContent = errorMessage(err);
    }
  });
}

export const reconciliationPage = {
  async render(view, ctx) {
    let experiments;
    try {
      experiments = await loadExperiments();
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => reconciliationPage.render(view, ctx));
      return;
    }
    if (!experiments.length) {
      view.innerHTML = `<section class="panel"><div class="panel-body">${errorBox('还没有实验')}</div></section>`;
      return;
    }
    const experiment = pickExperiment(experiments, ctx.query.experiment);
    if (!experiment) {
      view.innerHTML = `<section class="panel"><div class="panel-body">${errorBox('还没有实验')}</div></section>`;
      return;
    }
    state.experimentId = String(experiment.id);

    let summary;
    let cells;
    let latest;
    let batchList;
    try {
      const query = {};
      if (state.dateFrom) query.date_from = state.dateFrom;
      if (state.dateTo) query.date_to = state.dateTo;
      [summary, latest, batchList] = await Promise.all([
        api.get(`/api/experiments/${experiment.id}/reconciliation`, query),
        api.get(`/api/experiments/${experiment.id}/analysis-runs/latest`),
        api.get(`/api/experiments/${experiment.id}/return-batches`),
      ]);
      const cellQuery = { limit: 60 };
      if (state.status === 'blocking') cellQuery.status = 'delayed';
      else if (state.status && state.status !== 'all') cellQuery.status = state.status;
      cells = await api.get(`/api/experiments/${experiment.id}/reconciliation/cells`, cellQuery);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => reconciliationPage.render(view, ctx));
      return;
    }

    const canWrite = can(ctx.user.role, 'srm:run');
    const counts = summary.status_counts || {};
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>回传对账</h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">${summary.dates_total} 天 · ${summary.ready_dates} 天可参与分析</span>
            ${
              canWrite
                ? '<button class="btn sm ghost" id="ingest-return">接入回传</button><button class="btn sm accent" id="run-analysis">生成分析批次</button>'
                : '<span class="mono muted">只读角色仅可查看</span>'
            }
          </div>
        </div>
        <div class="panel-body">
          ${filterBar({
            id: 'reconcile-filters',
            fields: [
              field({
                label: '实验',
                name: 'experiment',
                value: experiment.id,
                options: experiments.map((item) => ({ value: item.id, label: `${item.code} · ${item.name}` })),
              }),
              field({
                label: '逐格状态',
                name: 'status',
                value: state.status,
                options: [
                  { value: 'all', label: '全部' },
                  { value: 'conflict', label: '仅冲突' },
                  { value: 'delayed', label: '仅延迟' },
                  { value: 'reconciled', label: '仅已和解' },
                  { value: 'complete', label: '仅齐全' },
                ],
              }),
              field({ label: '起（统计日）', name: 'date_from', type: 'date', value: state.dateFrom }),
              field({ label: '止（统计日）', name: 'date_to', type: 'date', value: state.dateTo }),
            ],
          })}
        </div>
      </section>

      ${statStrip([
        { label: '齐全', value: num(counts.complete || 0), unit: '格', hint: '两侧数值一致' },
        { label: '延迟', value: num(counts.delayed || 0), unit: '格', tone: counts.delayed ? 'mustard' : '', hint: '等另一侧回传' },
        { label: '冲突', value: num(counts.conflict || 0), unit: '格', tone: counts.conflict ? 'alarm' : '', hint: '需写解释 + 采用侧' },
        { label: '已和解', value: num(counts.reconciled || 0), unit: '格', hint: '差异可解释' },
      ])}

      ${sourcePanel(summary)}

      ${runPanel(experiment, latest)}

      <section class="panel">
        <div class="panel-head"><h2>按统计日核对</h2><span class="meta">${summary.all_ready ? '同批统计日齐全' : '存在阻断项'}</span></div>
        ${dayTable(summary)}
      </section>

      <section class="panel">
        <div class="panel-head"><h2>待处理：延迟与冲突</h2><span class="meta">${summary.blocking_total} 格</span></div>
        <div class="panel-body" style="padding:0">${blockingTable(summary.blocking, ctx)}</div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2>逐格对账明细</h2><span class="meta">${cells.total} 格 · 显示 ${cells.items.length}</span></div>
        ${table({
          rows: cells.items,
          columns: [
            { key: 'stat_date', label: '统计日', className: 'mono nowrap' },
            { key: 'variant_code', label: '变体', className: 'mono' },
            { key: 'channel_label', label: '渠道' },
            { key: 'metric_label', label: '指标' },
            {
              key: 'status',
              label: '状态',
              render: (row) => badge(reconcileTone(row.status), row.status_label),
            },
            {
              key: 'delta',
              label: '分流差 / 转化差',
              className: 'num',
              render: (row) =>
                `${row.sample_delta === null ? '—' : (row.sample_delta > 0 ? '+' : '') + num(row.sample_delta)} / ${
                  row.conversion_delta === null ? '—' : (row.conversion_delta > 0 ? '+' : '') + num(row.conversion_delta)
                }`,
            },
            {
              key: 'reason',
              label: '差异原因',
              render: (row) => esc(row.reason_label || ''),
            },
            {
              key: 'canonical',
              label: '采用值',
              className: 'num',
              render: (row) =>
                row.usable
                  ? `${num(row.canonical_samples)} / ${num(row.canonical_conversions)}<div class="mono muted">${esc(ENUMS.reconcileSourceShort[row.canonical_source] || '')}</div>`
                  : '<span class="muted">不可用</span>',
            },
            {
              key: 'explanation',
              label: '解释',
              render: (row) =>
                row.explanation ? `<span>${esc(row.explanation)}</span><div class="tiny muted">${esc(row.explained_by || '')}</div>` : '—',
            },
          ],
          empty: { title: '该筛选下没有对账格', hint: '换状态或日期范围。' },
        })}
      </section>

      <section class="panel">
        <div class="panel-head"><h2>双方批次回溯</h2><span class="meta">${batchList.active_total} 生效 / ${batchList.superseded_total} 已取代</span></div>
        <div class="panel-body" style="padding:0">
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>来源</th><th>统计日</th><th>批次号</th><th>revision</th><th>状态</th><th>行数</th><th>分流合计</th><th>转化合计</th><th>接收时间</th><th>备注</th></tr></thead>
              <tbody>
                ${batchList.items
                  .map(
                    (batch) => `<tr>
                      <td>${esc(ENUMS.reconcileSourceShort[batch.source] || batch.source)}</td>
                      <td class="mono">${esc(batch.stat_date)}</td>
                      <td class="mono">${esc(batch.batch_no)}</td>
                      <td class="num">${esc(batch.revision)}</td>
                      <td>${batch.status === 'active' ? badge('ok', '生效') : badge('pending', '已被新批次取代')}</td>
                      <td class="num">${num(batch.rows_count)}</td>
                      <td class="num">${num(batch.samples_total)}</td>
                      <td class="num">${num(batch.conversions_total)}</td>
                      <td class="mono">${fmtTime(batch.received_at)}</td>
                      <td class="muted">${esc(batch.note || '')}</td>
                    </tr>`,
                  )
                  .join('')}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    `;

    const filters = view.querySelector('#reconcile-filters');
    filters?.addEventListener('change', () => {
      state.status = filters.querySelector('[name=status]').value;
      state.dateFrom = filters.querySelector('[name=date_from]').value;
      state.dateTo = filters.querySelector('[name=date_to]').value;
      const next = filters.querySelector('[name=experiment]').value;
      location.hash = `#/reconciliation?experiment=${next}`;
      reconciliationPage.render(view, ctx);
    });

    view.querySelector('#ingest-return')?.addEventListener('click', () => openIngest(view, ctx, experiments, experiment, summary));
    view.querySelectorAll('[data-explain]').forEach((button) => {
      button.addEventListener('click', () => openExplain(view, ctx, button.dataset.explain, experiment));
    });
    view.querySelector('#run-analysis')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      button.textContent = '生成中…';
      try {
        const result = await api.post(`/api/experiments/${experiment.id}/analysis-runs`, { method: 'bh' });
        toast(
          result.reused
            ? `数据未变，沿用分析批次 #${result.run_no}`
            : `分析批次 #${result.run_no} 已生成：${result.dates_used.length} 天，重算 ${result.recomputed_dates.length} 天`,
          '',
        );
        await reconciliationPage.render(view, ctx);
      } catch (err) {
        toast(
          err.code === 'NOT_RECONCILED'
            ? '同批统计日还没齐全（有延迟或未解释的冲突），先处理阻断项'
            : errorMessage(err),
          'err',
        );
        button.disabled = false;
        button.textContent = '生成分析批次';
      }
    });
  },
};

export default reconciliationPage;
