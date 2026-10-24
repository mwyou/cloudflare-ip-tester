'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const date = value => value ? new Date(value * 1000).toLocaleString() : '—';
const phases = {starting:'准备启动',reading:'读取 DNS',read_done:'读取完成',checking:'检查线路',check_done:'检测完成',selecting:'优选新 IP',select_done:'优选完成',comparing:'六个结果排序',compare_done:'等待写入',healthy:'全部达标',updating:'写入 DNS',updated:'已完成同步',degraded:'合格 IP 不足',error:'任务未完成',stopped:'已停止',unconfigured:'等待部署配置',dry_run:'演练完成',cfst_changing:'切换 CFST',cfst_ready:'CFST 已就绪'};
let sending = false;
let actionError = '';
let timer;
let cfstData;
let cfstMessage = '';
let cfstOptions = '';
function disableActions() { document.querySelectorAll('[data-action],.cfst-controls button,.cfst-controls select').forEach(button => { button.disabled = true; }); }
function renderResultRows(selector, rows, ranked) {
  const body = $(selector);
  if (!rows?.length) {
    body.innerHTML = `<tr><td colspan="${ranked ? 5 : 4}" class="empty">暂无结果</td></tr>`;
    return;
  }
  body.innerHTML = rows.map(row => {
    const good = row.latency_ms <= currentStatus.max_latency_ms && row.bandwidth_mbps > currentStatus.min_bandwidth_mbps;
    const source = ranked ? `<td>${esc(row.source || '—')}</td>` : '';
    return `<tr>${source}<td>${esc(row.ip)}<small>${esc(row.colo || '—')}</small></td><td>${Number(row.latency_ms).toFixed(2)} ms</td><td>${Number(row.bandwidth_mbps).toFixed(2)} Mbps</td><td class="${good?'good':'bad'}">${good?'达标':row.speed_mibps>0?'未达标':'无下载样本'}</td></tr>`;
  }).join('');
}
let currentStatus = {max_latency_ms:100,min_bandwidth_mbps:30};
function renderCFST(data) {
  cfstData = data;
  const info = data.cfst;
  const current = info?.installed.find(item => item.id === info.current);
  $('#cfst-current').textContent = current?.version || data.engine || '未配置';
  const options = info ? [
    ...info.installed.map(item => ({value:`installed:${item.id}`, label:`${item.version}${item.current?'（当前）':'（已安装）'}`})),
    ...info.releases.filter(item => !info.installed.some(local => local.version === item.version)).map(item => ({value:`release:${item.version}`,label:item.version}))
  ] : [];
  const key = JSON.stringify(options);
  const select = $('#cfst-version');
  if (key !== cfstOptions) {
    const selected = select.value;
    select.innerHTML = options.length ? options.map(item => `<option value="${esc(item.value)}">${esc(item.label)}</option>`).join('') : '<option value="">暂无版本</option>';
    select.value = options.some(item => item.value === selected) ? selected : current ? `installed:${current.id}` : options[0]?.value || '';
    cfstOptions = key;
  }
  const disabled = sending || !info || !data.manual_enabled || data.busy || Boolean(info.error);
  select.disabled = disabled || !options.length;
  $('#cfst-refresh').disabled = disabled;
  $('#cfst-apply').textContent = select.value.startsWith('release:') ? '安装并切换' : '切换版本';
  $('#cfst-apply').disabled = disabled || !select.value || select.value === `installed:${info?.current}`;
  $('#cfst-rollback').disabled = disabled || !info?.previous;
  $('#cfst-status').textContent = !info ? '部署后可管理版本' : info.error || (data.active_action?.startsWith('cfst_') ? data.message : cfstMessage || data.cfst_message || (info.checked_at ? `版本检查：${date(info.checked_at)}` : ''));
}
async function refresh() {
  clearTimeout(timer);
  try {
    const response = await fetch('/api/status',{cache:'no-store'});
    if (!response.ok) throw new Error('status');
    const data = await response.json();
    currentStatus = data;
    renderCFST(data);
    $('#connection').textContent = data.phase === 'unconfigured' ? '预览' : '已连接';
    $('#domain').textContent = data.domain || '未配置';
    $('#kind').textContent = `${data.record_type} 记录`;
    $('#threshold').textContent = `≤ ${data.max_latency_ms} ms`;
    $('#bandwidth').textContent = `> ${data.min_bandwidth_mbps} Mbps`;
    $('#next').textContent = data.next_run ? new Date(data.next_run*1000).toLocaleTimeString() : '—';
    $('#last').textContent = data.last_run ? `上次：${date(data.last_run)}` : `每 ${data.interval_seconds/3600} 小时检查`;
    const intervalInput = $('#interval-minutes');
    if (document.activeElement !== intervalInput) intervalInput.value = Math.round(data.interval_seconds / 60);
    $('#interval-status').textContent = data.busy ? '任务运行中，稍后可修改' : `当前每 ${Math.round(data.interval_seconds / 60)} 分钟检查一次`;
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
      const countLabel = action === 'read' || action === 'write' ? '个 IP' : '个达标';
      $(`#step-${action}`).textContent = !data.manual_enabled ? '—' : running ? '运行中…' : (reason && !data.busy ? reason : step ? `已完成 · ${step.count} ${countLabel}` : data.busy ? '等待任务完成' : '可启动');
    });
    $('#proposal').hidden = !data.proposed_ips?.length;
    $('#proposal').textContent = data.proposed_ips?.length ? `待写入：${data.proposed_ips.join('、')}` : '';
    $('#dns').innerHTML = data.dns_ips.length ? data.dns_ips.map(ip => `<div class="dns-item">${esc(ip)}</div>`).join('') : '<p class="muted">暂无记录</p>';
    $('#updated').textContent = data.last_dns_update ? `更新于 ${date(data.last_dns_update)}` : '尚未更新';
    const change = data.dns_change;
    $('#dns-change').innerHTML = change ? `<strong>上次变更</strong><br>之前：${esc((change.before || []).join('、') || '无')}<br>之后：${esc((change.after || []).join('、') || '无')}<br><span class="muted">${esc(date(change.time))}</span>` : '暂无变更对比';
    renderResultRows('#check-rows', data.check_results, false);
    renderResultRows('#selection-rows', (data.selection_results || []).slice(0, 10), false);
    renderResultRows('#ranked-rows', data.ranked_results, true);
    $('#events').innerHTML = data.events.length ? [...data.events].reverse().map(event=>`<li><time>${esc(date(event.time))}</time><span>${esc(event.message)}</span></li>`).join('') : '<li class="muted">等待首次检查</li>';
  } catch {
    disableActions();
    $('#connection').textContent = '状态连接中断';
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
$('#cfst-version').addEventListener('change', () => { if (cfstData) renderCFST(cfstData); });
async function cfstRequest(action) {
  if (sending) return;
  sending = true;
  cfstMessage = '';
  disableActions();
  $('#cfst-status').textContent = action === 'refresh' ? '正在检查官方版本…' : '正在提交…';
  try {
    let response;
    if (action === 'refresh') {
      response = await fetch('/api/cfst/releases', {cache:'no-store'});
    } else {
      const selected = $('#cfst-version').value;
      const payload = action === 'rollback' ? {action:'rollback'} : selected.startsWith('release:') ?
        {action:'install',version:selected.slice(8)} : {action:'switch',release_id:selected.slice(10)};
      response = await fetch('/api/cfst', {method:'POST',headers:{'Content-Type':'application/json','X-Local-Client':'cfst-dashboard'},body:JSON.stringify(payload)});
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'CFST 操作失败');
    cfstMessage = action === 'refresh' ? '已获取官方版本' : '';
  } catch (error) { cfstMessage = error.message || 'CFST 操作失败'; }
  finally { sending = false; await refresh(); }
}
$('#cfst-refresh').addEventListener('click', () => cfstRequest('refresh'));
$('#cfst-apply').addEventListener('click', () => cfstRequest('apply'));
$('#cfst-rollback').addEventListener('click', () => cfstRequest('rollback'));
$('#interval-save').addEventListener('click', async () => {
  if (sending) return;
  const minutes = Number($('#interval-minutes').value);
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) {
    $('#interval-status').textContent = '请输入 1 到 1440 分钟';
    return;
  }
  sending = true;
  disableActions();
  $('#interval-save').disabled = true;
  try {
    const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json','X-Local-Client':'cfst-dashboard'}, body:JSON.stringify({interval_seconds:minutes*60})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
  } catch (error) {
    $('#interval-status').textContent = error.message || '保存失败';
  } finally {
    sending = false;
    await refresh();
  }
});
refresh();
