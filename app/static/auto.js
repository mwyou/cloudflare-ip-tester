'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const date = value => value ? new Date(value * 1000).toLocaleString() : '—';
const phases = {starting:'准备启动',reading:'读取 DNS',read_done:'读取完成',checking:'检查线路',check_done:'检测完成',selecting:'优选新 IP',select_done:'优选完成',comparing:'综合排序',compare_done:'等待写入',healthy:'全部达标',updating:'写入 DNS',updated:'已完成同步',degraded:'结果不足',error:'任务未完成',stopped:'已停止',unconfigured:'等待部署配置',dry_run:'演练完成',testing_ip:'单 IP 测速',ip_test_done:'单 IP 测速完成',settings_saved:'设置已更新'};
let sending = false;
let actionError = '';
let timer;
let intervalMessage = '';
let recordCountMessage = '';
let historyCountMessage = '';
let jitterMessage = '';
let thresholdMessage = '';
let thresholdDirty = false;
const settingDrafts = new Set();
let jitterDirty = false;
const resultExpanded = {selection: false, ranked: false};
function testButton(ip) {
  const running = currentStatus.busy && currentStatus.active_ip === ip;
  const disabled = sending || currentStatus.busy || !currentStatus.manual_enabled || currentStatus.phase === 'stopped';
  return `<button type="button" class="ip-test${running?' running':''}" data-test-ip="${esc(ip)}" aria-label="测速 ${esc(ip)}" ${disabled?'disabled':''}>${running?'测速中':'测速'}</button>`;
}
function dnsItems(ips) {
  return ips?.length ? ips.map(ip => {
    const sample = currentStatus.single_results?.[ip];
    const result = sample?.result;
    const detail = sample ? sample.error || (result?.measured === false ? '无有效样本' : `${Number(result.latency_ms).toFixed(2)} ms · ${Number(result.bandwidth_mbps).toFixed(2)} Mbps`) : '';
    return `<div class="dns-item"><div class="ip-line"><span>${esc(ip)}</span>${testButton(ip)}</div>${sample?`<small class="single-result">${esc(detail)}<br>${esc(date(sample.time))}</small>`:''}</div>`;
  }).join('') : '<p class="muted">暂无记录</p>';
}
function disableActions() { document.querySelectorAll('[data-action],[data-test-ip],.settings-panel button,.settings-panel select,.settings-panel input').forEach(button => { button.disabled = true; }); }
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
  const disabled = sending || currentStatus.busy || !currentStatus.manual_enabled;
  $('#jitter-enabled').disabled = $('#jitter-save').disabled = disabled;
  $('#jitter-min').disabled = $('#jitter-max').disabled = disabled || !enabled;
}
async function refresh() {
  clearTimeout(timer);
  try {
    const response = await fetch('/api/status',{cache:'no-store'});
    if (!response.ok) throw new Error('status');
    const data = await response.json();
    currentStatus = data;
    $('#connection').classList.remove('offline');
    $('#connection').textContent = data.phase === 'unconfigured' ? '预览' : '已连接';
    $('#domain').textContent = data.domain || '未配置';
    $('#kind').textContent = `${data.record_type} 记录`;
    $('#threshold').textContent = `≤ ${data.max_latency_ms} ms`;
    $('#bandwidth').textContent = `> ${data.min_bandwidth_mbps} Mbps`;
    $('#next').textContent = data.next_run ? new Date(data.next_run*1000).toLocaleTimeString() : '—';
    $('#next').title = data.next_run ? date(data.next_run) : '';
    $('#last').textContent = data.last_run ? `上次：${date(data.last_run)}` : `每 ${data.interval_seconds/3600} 小时检查`;
    const intervalInput = $('#interval-minutes');
    if (!settingDrafts.has('#interval-minutes') && document.activeElement !== intervalInput) intervalInput.value = Math.round(data.interval_seconds / 60);
    intervalInput.disabled = $('#interval-save').disabled = sending || data.busy || !data.manual_enabled;
    $('#interval-status').textContent = intervalMessage;
    const recordCountInput = $('#dns-record-count');
    if (!settingDrafts.has('#dns-record-count') && document.activeElement !== recordCountInput) recordCountInput.value = data.dns_record_count ?? 3;
    recordCountInput.disabled = $('#dns-record-count-save').disabled = sending || data.busy || !data.manual_enabled;
    $('#dns-record-count-status').textContent = recordCountMessage;
    const historyCountInput = $('#dns-history-count');
    if (!settingDrafts.has('#dns-history-count') && document.activeElement !== historyCountInput) historyCountInput.value = data.dns_history_count ?? 2;
    historyCountInput.disabled = $('#dns-history-count-save').disabled = sending || data.busy || !data.manual_enabled;
    $('#dns-history-count-status').textContent = historyCountMessage || '0 表示不保留';
    if (!jitterDirty) {
      const jitterMax = Number(data.jitter_max_seconds ?? 0);
      $('#jitter-enabled').checked = jitterMax > 0;
      if (jitterMax > 0) {
        $('#jitter-min').value = data.jitter_min_seconds;
        $('#jitter-max').value = jitterMax;
      }
    }
    renderJitter();
    $('#jitter-status').textContent = jitterMessage || '在检查间隔上随机增加时间';
    if (!thresholdDirty) {
      $('#max-latency').value = data.max_latency_ms;
      $('#min-bandwidth').value = data.min_bandwidth_mbps;
    }
    $('#max-latency').disabled = $('#min-bandwidth').disabled = $('#threshold-save').disabled = sending || data.busy || !data.manual_enabled;
    $('#threshold-status').textContent = thresholdMessage;
    $('#phase').textContent = phases[data.phase] || data.phase;
    $('#notice').textContent = actionError || (data.phase === 'unconfigured' ? '预览模式，请先配置部署。' : data.message);
    $('#notice').className = actionError || ['error','degraded','unconfigured','stopped'].includes(data.phase) ? 'notice error' : 'notice';
    document.querySelectorAll('[data-action]').forEach(button => {
      const action = button.dataset.action;
      const reason = !data.manual_enabled ? '请先配置部署' : data.actions?.[action];
      const step = data.steps?.[action];
      const running = data.busy && data.active_action === action;
      button.disabled = sending || !data.manual_enabled || data.busy || Boolean(reason);
      button.classList.toggle('running', running);
      button.title = reason || '';
      const countLabel = action === 'select' ? '条结果' : action === 'read' || action === 'write' || action === 'compare' ? '个 IP' : '个达标';
      $(`#step-${action}`).textContent = !data.manual_enabled ? '—' : running ? '运行中…' : (reason && !data.busy ? reason : step ? `已完成 · ${step.count} ${countLabel}` : data.busy ? '等待任务完成' : '可启动');
    });
    $('#proposal').hidden = !data.proposed_ips?.length;
    $('#proposal').textContent = data.proposed_ips?.length ? `待写入：${data.proposed_ips.join('、')}` : '';
    $('#dns').innerHTML = dnsItems(data.dns_ips);
    $('#updated').textContent = data.last_dns_update ? `更新于 ${date(data.last_dns_update)}` : '尚未更新';
    const historyCount = data.dns_history_count ?? 2;
    $('#history-count-pill').textContent = historyCount === 0 ? '未保存' : `最近 ${historyCount} 次`;
    const history = (data.dns_history || []).slice(0, historyCount);
    $('#dns-history').innerHTML = history.length ? history.map((entry, index) =>
      `<section class="dns-snapshot"><div class="snapshot-title"><strong>${index === 0 ? '上次变更前' : `前 ${index + 1} 次变更前`}</strong></div><time>变更于 ${esc(date(entry.time))}</time><div class="dns-list">${dnsItems(entry.ips)}</div></section>`
    ).join('') : `<p class="muted">${historyCount === 0 ? '已关闭历史记录' : '暂无历史记录'}</p>`;
    renderResultRows('#check-rows', data.check_results, false);
    // Use the DNS snapshot from selection time: writing the winners into DNS
    // must not make those new results disappear from the completed run.
    const oldIPs = new Set(data.selection_existing_ips ?? (data.check_results || []).map(row => row.ip));
    const newRows = (data.selection_results || []).filter(row => !oldIPs.has(row.ip));
    renderResultRows('#selection-rows', newRows, false, '#selection-toggle', resultExpanded.selection);
    renderResultRows('#ranked-rows', data.ranked_results, true, '#ranked-toggle', resultExpanded.ranked);
    $('#check-time').textContent = data.check_time ? date(data.check_time) : '';
    $('#selection-count').textContent = data.selection_time ? `候选 ${data.selection_candidate_count ?? 0} 个 · 新 IP ${newRows.length} 条` : '';
    $('#selection-time').textContent = data.selection_time ? date(data.selection_time) : '';
    $('#ranked-time').textContent = data.ranked_time ? date(data.ranked_time) : '';
    $('#events').innerHTML = data.events.length ? [...data.events].reverse().map(event=>`<li><time>${esc(date(event.time))}</time><span>${esc(event.message)}</span></li>`).join('') : '<li class="muted">等待首次检查</li>';
  } catch {
    disableActions();
    $('#connection').classList.add('offline');
    $('#connection').textContent = '连接中断';
    $('#notice').textContent = '无法连接状态服务，正在自动重连；请检查 Docker 容器日志。';
    $('#notice').className = 'notice error';
  } finally { timer = setTimeout(refresh,3000); }
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
for (const selector of ['#max-latency', '#min-bandwidth']) {
  $(selector).addEventListener('input', () => { thresholdDirty = true; });
}
$('#threshold-save').addEventListener('click', async () => {
  if (sending) return;
  const latency = Number($('#max-latency').value);
  const bandwidth = Number($('#min-bandwidth').value);
  if (!Number.isFinite(latency) || !Number.isFinite(bandwidth) || latency < 1 || latency > 2000 || bandwidth < .1 || bandwidth > 10000) {
    thresholdMessage = '延迟 1～2000 ms；带宽 0.1～10000 Mbps';
    $('#threshold-status').textContent = thresholdMessage;
    return;
  }
  sending = true;
  thresholdMessage = '';
  disableActions();
  try {
    const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json','X-Local-Client':'cfst-dashboard'}, body:JSON.stringify({max_latency_ms:latency,min_bandwidth_mbps:bandwidth})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    thresholdDirty = false;
  } catch (error) { thresholdMessage = error.message || '保存失败'; }
  finally { sending = false; await refresh(); }
});
function bindSetting({button, input, status, field, min, max, scale = 1, error, setMessage}) {
  $(input).addEventListener('input', () => settingDrafts.add(input));
  $(button).addEventListener('click', async () => {
    if (sending) return;
    const value = Number($(input).value);
    if ($(input).value === '' || !Number.isInteger(value) || value < min || value > max) {
      setMessage(error);
      $(status).textContent = error;
      return;
    }
    sending = true;
    setMessage('');
    disableActions();
    try {
      const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json','X-Local-Client':'cfst-dashboard'}, body:JSON.stringify({[field]:value * scale})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '保存失败');
      settingDrafts.delete(input);
    } catch (error) {
      setMessage(error.message || '保存失败');
    } finally {
      sending = false;
      await refresh();
    }
  });
}
bindSetting({button:'#interval-save', input:'#interval-minutes', status:'#interval-status', field:'interval_seconds', min:1, max:1440, scale:60, error:'请输入 1 到 1440 分钟', setMessage:message => { intervalMessage = message; }});
bindSetting({button:'#dns-record-count-save', input:'#dns-record-count', status:'#dns-record-count-status', field:'dns_record_count', min:1, max:10, error:'请输入 1 到 10 条', setMessage:message => { recordCountMessage = message; }});
bindSetting({button:'#dns-history-count-save', input:'#dns-history-count', status:'#dns-history-count-status', field:'dns_history_count', min:0, max:10, error:'请输入 0 到 10 份', setMessage:message => { historyCountMessage = message; }});
function adjustJitter(changed) {
  jitterDirty = true;
  const minimum = Number($('#jitter-min').value);
  const maximum = Number($('#jitter-max').value);
  if (minimum > maximum) {
    if (changed === 'min') $('#jitter-max').value = minimum;
    else $('#jitter-min').value = maximum;
  }
  renderJitter();
}
$('#jitter-enabled').addEventListener('change', () => { jitterDirty = true; renderJitter(); });
$('#jitter-min').addEventListener('input', () => adjustJitter('min'));
$('#jitter-max').addEventListener('input', () => adjustJitter('max'));
$('#jitter-save').addEventListener('click', async () => {
  if (sending) return;
  const enabled = $('#jitter-enabled').checked;
  const minimum = enabled ? Number($('#jitter-min').value) : 0;
  const maximum = enabled ? Number($('#jitter-max').value) : 0;
  if (!Number.isInteger(minimum) || !Number.isInteger(maximum) || (enabled && minimum < 1) || maximum > 10000 || minimum > maximum) {
    jitterMessage = '随机波动区间无效';
    $('#jitter-status').textContent = jitterMessage;
    return;
  }
  sending = true;
  jitterMessage = '';
  disableActions();
  try {
    const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json','X-Local-Client':'cfst-dashboard'}, body:JSON.stringify({jitter_min_seconds:minimum,jitter_max_seconds:maximum})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    jitterDirty = false;
  } catch (error) {
    jitterMessage = error.message || '保存失败';
  } finally {
    sending = false;
    await refresh();
  }
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
