'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const date = value => value ? new Date(value * 1000).toLocaleString() : '—';
const phases = {starting:'准备启动',reading:'读取 DNS',read_done:'读取完成',checking:'检查线路',check_done:'检测完成',selecting:'优选新 IP',select_done:'优选完成',comparing:'合并复测',compare_done:'等待写入',healthy:'全部达标',updating:'写入 DNS',updated:'已完成同步',degraded:'合格 IP 不足',error:'任务未完成',stopped:'已停止',unconfigured:'等待部署配置',dry_run:'演练完成'};
let sending = false;
let actionError = '';
let timer;
function disableActions() { document.querySelectorAll('[data-action]').forEach(button => { button.disabled = true; }); }
async function refresh() {
  clearTimeout(timer);
  try {
    const response = await fetch('/api/status',{cache:'no-store'});
    if (!response.ok) throw new Error('status');
    const data = await response.json();
    $('#connection').textContent = data.phase === 'unconfigured' ? '预览' : '已连接';
    $('#domain').textContent = data.domain || '未配置';
    $('#kind').textContent = `${data.record_type} 记录`;
    $('#threshold').textContent = `≤ ${data.max_latency_ms} ms`;
    $('#bandwidth').textContent = `> ${data.min_bandwidth_mbps} Mbps`;
    $('#next').textContent = data.next_run ? new Date(data.next_run*1000).toLocaleTimeString() : '—';
    $('#last').textContent = data.last_run ? `上次：${date(data.last_run)}` : `每 ${data.interval_seconds/3600} 小时检查`;
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
    $('#rows').innerHTML = data.results.length ? data.results.map(row => {
      const good = row.latency_ms <= data.max_latency_ms && row.bandwidth_mbps > data.min_bandwidth_mbps;
      return `<tr><td>${esc(row.ip)}<small>${esc(row.colo || '—')}</small></td><td>${Number(row.latency_ms).toFixed(2)} ms</td><td>${Number(row.bandwidth_mbps).toFixed(2)} Mbps</td><td class="${good?'good':'bad'}">${good?'达标':row.speed_mibps>0?'未达标':'无下载样本'}</td></tr>`;
    }).join('') : '<tr><td colspan="4" class="empty">暂无测速结果</td></tr>';
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
refresh();
