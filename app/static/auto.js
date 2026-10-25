'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const date = value => value ? new Date(value * 1000).toLocaleString() : '—';
const phases = {starting:'准备启动',reading:'读取 DNS',read_done:'读取完成',checking:'检查线路',check_done:'检测完成',selecting:'优选新 IP',select_done:'优选完成',comparing:'综合排序',compare_done:'等待写入',healthy:'全部达标',updating:'写入 DNS',updated:'已完成同步',degraded:'结果不足',error:'任务未完成',stopped:'已停止',unconfigured:'等待部署配置',dry_run:'演练完成',testing_ip:'单 IP 测速',ip_test_done:'单 IP 测速完成',settings_saved:'设置已更新'};
let sending = false;
let actionError = '';
let timer;
let settingsDirty = false;
let refreshing = null;
let refreshAgain = false;
let controlsKey = '';
const rendered = {};
const resultExpanded = {selection: false, ranked: false};
const setText = (selector, value) => {
  const element = $(selector);
  if (element.textContent !== String(value ?? '')) element.textContent = value ?? '';
};
function testButton(ip) {
  const running = currentStatus.busy && currentStatus.active_ip === ip;
  const disabled = sending || currentStatus.busy || !currentStatus.manual_enabled || currentStatus.phase === 'stopped';
  return `<button type="button" class="ip-test${running?' running':''}" data-test-ip="${esc(ip)}" aria-label="测速 ${esc(ip)}" ${disabled?'disabled':''}>${running?'测速中':'测速'}</button>`;
}
function dnsItems(ips) {
  return ips?.length ? ips.map(ip => `<div class="dns-item">${esc(ip)}</div>`).join('') : '<p class="muted">暂无记录</p>';
}
function disableActions() { controlsKey = ''; document.querySelectorAll('[data-action],[data-test-ip],.settings-panel button,.settings-panel select,.settings-panel input').forEach(button => { button.disabled = true; }); }
function renderResultRows(selector, rows, ranked, toggleSelector, expanded) {
  const body = $(selector);
  if (!rows?.length) {
    body.innerHTML = `<tr><td colspan="${ranked ? 5 : 4}" class="empty">暂无结果</td></tr>`;
    const toggle = toggleSelector && $(toggleSelector);
    if (toggle) toggle.hidden = true;
    return;
  }
  body.innerHTML = rows.map(row => {
    const good = row.latency_ms <= currentStatus.max_latency_ms && row.bandwidth_mbps > currentStatus.min_bandwidth_mbps;
    const singleError = currentStatus.single_results?.[row.ip]?.error;
    const source = ranked ? `<td>${esc(row.source || '—')}</td>` : '';
    return `<tr class="${row.selected?'selected':''}">${source}<td class="ip-cell"><div class="ip-line"><span>${esc(row.ip)}</span>${testButton(row.ip)}</div><small>${esc(row.colo || '—')}${row.tested_at?` · 单测 ${esc(date(row.tested_at))}`:''}</small>${singleError?`<small class="bad">${esc(singleError)}</small>`:''}</td><td>${row.measured === false ? '—' : Number(row.latency_ms).toFixed(2)+' ms'}</td><td>${row.measured === false ? '—' : Number(row.bandwidth_mbps).toFixed(2)+' Mbps'}</td><td class="${good?'good':'bad'}">${row.selected?'入选':good?'达标':row.measured===false?'无有效样本':row.speed_mibps>0?'未达标':'无下载样本'}</td></tr>`;
  }).join('');
  body.classList.toggle('collapsed', Boolean(toggleSelector) && !expanded && rows.length > 3);
  const toggle = toggleSelector && $(toggleSelector);
  if (toggle) {
    toggle.hidden = rows.length <= 3;
    toggle.textContent = expanded ? '收起' : '展开全部';
    toggle.setAttribute('aria-expanded', String(expanded));
  }
}
let currentStatus = {max_latency_ms:100,min_bandwidth_mbps:30};
function renderJitter() {
  const enabled = $('#jitter-enabled').checked;
  const minimum = Number($('#jitter-min').value);
  const maximum = Number($('#jitter-max').value);
  $('#jitter-value').textContent = enabled ? `${minimum}–${maximum} 秒` : '关闭';
  $('#jitter-range').classList.toggle('off', !enabled);
  const low = (minimum - 1) / 9999 * 100;
  const width = (maximum - minimum) / 9999 * 100;
  $('#jitter-fill').style.left = `calc(${low}% + ${8 - .16 * low}px)`;
  $('#jitter-fill').style.width = `calc(${width}% - ${.16 * width}px)`;
  const disabled = sending || currentStatus.busy || !currentStatus.manual_enabled || currentStatus.phase === 'stopped';
  $('#jitter-enabled').disabled = disabled;
  $('#jitter-min').disabled = $('#jitter-max').disabled = disabled || !enabled;
}
function renderSettings(data) {
  if (settingsDirty) return;
  $('#interval-minutes').value = Math.round(data.interval_seconds / 60);
  $('#dns-record-count').value = data.dns_record_count ?? 3;
  $('#dns-history-count').value = data.dns_history_count ?? 2;
  $('#max-latency').value = data.max_latency_ms;
  $('#min-bandwidth').value = data.min_bandwidth_mbps;
  const maximum = Number(data.jitter_max_seconds ?? 0);
  $('#jitter-enabled').checked = maximum > 0;
  if (maximum > 0) {
    $('#jitter-min').value = data.jitter_min_seconds;
    $('#jitter-max').value = maximum;
  }
  renderJitter();
}
function renderSection(key, data) {
  if (key === 'settings') renderSettings(data);
  if (key === 'dns') {
    $('#dns').innerHTML = dnsItems(data.dns_ips);
    setText('#updated', data.last_dns_update ? `更新于 ${date(data.last_dns_update)}` : '尚未更新');
    const count = data.dns_history_count ?? 2;
    setText('#history-count-pill', count === 0 ? '未保存' : `最近 ${count} 次`);
    const history = (data.dns_history || []).slice(0, count);
    $('#dns-history').innerHTML = history.length ? history.map((entry, index) =>
      `<section class="dns-snapshot"><div class="snapshot-title"><strong>${index === 0 ? '上次变更前' : `前 ${index + 1} 次变更前`}</strong></div><time>变更于 ${esc(date(entry.time))}</time><div class="dns-list">${dnsItems(entry.ips)}</div></section>`
    ).join('') : `<p class="muted">${count === 0 ? '已关闭历史记录' : '暂无历史记录'}</p>`;
  }
  if (key === 'check') {
    renderResultRows('#check-rows', data.check_results, false);
    setText('#check-time', data.check_time ? date(data.check_time) : '');
  }
  if (key === 'selection') {
    // Keep the selection-time DNS snapshot even after winners are written.
    const oldIPs = new Set(data.selection_existing_ips ?? []);
    const newRows = (data.selection_results || []).filter(row => !oldIPs.has(row.ip));
    renderResultRows('#selection-rows', newRows, false, '#selection-toggle', resultExpanded.selection);
    setText('#selection-count', data.selection_time ? `候选 ${data.selection_candidate_count ?? 0} 个 · 新 IP ${newRows.length} 条` : '');
    setText('#selection-time', data.selection_time ? date(data.selection_time) : '');
  }
  if (key === 'ranked') {
    $('#proposal').hidden = !data.proposed_ips?.length;
    setText('#proposal', data.proposed_ips?.length ? `待写入：${data.proposed_ips.join('、')}` : '');
    renderResultRows('#ranked-rows', data.ranked_results, true, '#ranked-toggle', resultExpanded.ranked);
    setText('#ranked-time', data.ranked_time ? date(data.ranked_time) : '');
  }
  if (key === 'events') {
    $('#events').innerHTML = data.events.length ? [...data.events].reverse().map(event=>`<li><time>${esc(date(event.time))}</time><span>${esc(event.message)}</span></li>`).join('') : '<li class="muted">等待首次检查</li>';
  }
}
function renderOverview(data) {
  $('#connection').classList.remove('offline');
  setText('#connection', data.phase === 'unconfigured' ? '预览' : '已连接');
  setText('#domain', data.domain || '未配置');
  setText('#kind', `${data.record_type} 记录`);
  setText('#threshold', `≤ ${data.max_latency_ms} ms`);
  setText('#bandwidth', `> ${data.min_bandwidth_mbps} Mbps`);
  setText('#next', data.next_run ? new Date(data.next_run*1000).toLocaleTimeString() : '—');
  $('#next').title = data.next_run ? date(data.next_run) : '';
  setText('#last', data.last_run ? `上次：${date(data.last_run)}` : `每 ${data.interval_seconds/60} 分钟检查`);
  setText('#phase', phases[data.phase] || data.phase);
  setText('#notice', actionError || (data.phase === 'unconfigured' ? '预览模式，请先配置部署。' : data.message));
  $('#notice').className = actionError || ['error','degraded','unconfigured','stopped'].includes(data.phase) ? 'notice error' : 'notice';
}
function renderControls(data) {
  const key = JSON.stringify([sending, data.busy, data.manual_enabled, data.phase === 'stopped', data.active_action, data.active_ip, data.actions, data.steps]);
  if (key === controlsKey) return;
  controlsKey = key;
  document.querySelectorAll('[data-action]').forEach(button => {
    const action = button.dataset.action;
    const reason = !data.manual_enabled ? '请先配置部署' : data.actions?.[action];
    const step = data.steps?.[action];
    const running = data.busy && data.active_action === action;
    button.disabled = sending || !data.manual_enabled || data.busy || Boolean(reason);
    button.classList.toggle('running', running);
    button.title = reason || '';
    const countLabel = action === 'select' ? '条结果' : ['read','write','compare'].includes(action) ? '个 IP' : '个达标';
    setText(`#step-${action}`, !data.manual_enabled ? '—' : running ? '运行中…' : (reason && !data.busy ? reason : step ? `已完成 · ${step.count} ${countLabel}` : data.busy ? '等待任务完成' : '可启动'));
  });
  document.querySelectorAll('[data-test-ip]').forEach(button => {
    const running = data.busy && data.active_ip === button.dataset.testIp;
    button.disabled = sending || data.busy || !data.manual_enabled || data.phase === 'stopped';
    button.classList.toggle('running', running);
    const text = running ? '测速中' : '测速';
    if (button.textContent !== text) button.textContent = text;
  });
  document.querySelectorAll('.settings-panel button,.settings-panel input').forEach(input => {
    input.disabled = sending || data.busy || !data.manual_enabled || data.phase === 'stopped';
  });
  renderJitter();
}
async function fetchStatus(path) {
  const response = await fetch(path, {cache:'no-store'});
  if (!response.ok) throw new Error('status');
  return response.json();
}
async function refresh() {
  if (refreshing) { refreshAgain = true; return refreshing; }
  clearTimeout(timer);
  refreshing = (async () => {
    do {
      refreshAgain = false;
      try {
        let data = await fetchStatus('/api/status/summary');
        // Only fetch result data when a section changed, including background
        // jobs, other browsers and server restarts. Idle polls stay small.
        if (Object.entries(data.revisions).some(([key, revision]) => rendered[key] !== revision)) {
          data = await fetchStatus('/api/status');
          currentStatus = data;
          for (const [key, revision] of Object.entries(data.revisions)) {
            if (rendered[key] !== revision) {
              renderSection(key, data);
              rendered[key] = revision;
            }
          }
        } else currentStatus = {...currentStatus, ...data};
        renderOverview(data);
        renderControls(data);
      } catch {
        disableActions();
        $('#connection').classList.add('offline');
        setText('#connection', '连接中断');
        setText('#notice', '无法连接状态服务，正在自动重连；请检查 Docker 容器日志。');
        $('#notice').className = 'notice error';
      }
    } while (refreshAgain);
  })();
  try { await refreshing; }
  finally { refreshing = null; timer = setTimeout(refresh,3000); }
}

document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', async () => {
  if (sending) return;
  sending = true;
  actionError = '';
  disableActions();
  try {
    const response = await fetch('/api/actions', {method:'POST', headers:{'Content-Type':'application/json','X-Local-Client':'cfst-dashboard'}, body:JSON.stringify({action:button.dataset.action})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '启动失败');
  } catch (error) {
    actionError = error.message || '无法启动，请检查连接';
  } finally {
    sending = false;
    await refresh();
  }
}));
document.addEventListener('click', async event => {
  const button = event.target.closest('button[data-test-ip]');
  if (!button || sending || button.disabled) return;
  sending = true;
  actionError = '';
  disableActions();
  try {
    const response = await fetch('/api/test-ip', {method:'POST', headers:{'Content-Type':'application/json','X-Local-Client':'cfst-dashboard'}, body:JSON.stringify({ip:button.dataset.testIp})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '测速启动失败');
  } catch (error) { actionError = error.message || '测速启动失败'; }
  finally { sending = false; await refresh(); }
});
$('#settings-form').addEventListener('input', () => {
  settingsDirty = true;
  setText('#settings-status', '有未保存的更改');
});
function adjustJitter(changed) {
  const minimum = Number($('#jitter-min').value);
  const maximum = Number($('#jitter-max').value);
  if (minimum > maximum) {
    if (changed === 'min') $('#jitter-max').value = minimum;
    else $('#jitter-min').value = maximum;
  }
  renderJitter();
}
$('#jitter-enabled').addEventListener('change', renderJitter);
$('#jitter-min').addEventListener('input', () => adjustJitter('min'));
$('#jitter-max').addEventListener('input', () => adjustJitter('max'));
$('#settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (sending || !event.currentTarget.reportValidity()) return;
  const enabled = $('#jitter-enabled').checked;
  const settings = {
    interval_seconds: Number($('#interval-minutes').value) * 60,
    jitter_min_seconds: enabled ? Number($('#jitter-min').value) : 0,
    jitter_max_seconds: enabled ? Number($('#jitter-max').value) : 0,
    max_latency_ms: Number($('#max-latency').value),
    min_bandwidth_mbps: Number($('#min-bandwidth').value),
    dns_record_count: Number($('#dns-record-count').value),
    dns_history_count: Number($('#dns-history-count').value)
  };
  sending = true;
  setText('#settings-status', '保存中…');
  disableActions();
  try {
    const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json','X-Local-Client':'cfst-dashboard'}, body:JSON.stringify(settings)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    settingsDirty = false;
    delete rendered.settings;
    setText('#settings-status', '已保存');
  } catch (error) { setText('#settings-status', error.message || '保存失败'); }
  finally { sending = false; await refresh(); }
});
for (const key of Object.keys(resultExpanded)) {
  $(`#${key}-toggle`).addEventListener('click', event => {
    resultExpanded[key] = !resultExpanded[key];
    event.currentTarget.textContent = resultExpanded[key] ? '收起' : '展开全部';
    event.currentTarget.setAttribute('aria-expanded', String(resultExpanded[key]));
    const section = event.currentTarget.closest('.result-section');
    const returnToTitle = !resultExpanded[key] && section.getBoundingClientRect().top < 0;
    $(`#${key}-rows`).classList.toggle('collapsed', !resultExpanded[key]);
    if (returnToTitle) section.scrollIntoView({block:'start'});
  });
}
refresh();
