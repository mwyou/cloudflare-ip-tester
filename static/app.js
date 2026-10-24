'use strict';
const $ = s => document.querySelector(s);
const state = { snapshot: null, selected: new Set(), job: null, busy: false, fetching: false, pollFailures: 0 };
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const speed = value => Number(value).toFixed(2);
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Client': 'cfip-desk' }, body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function notice(text, type = '') { $('#notice').textContent = text; $('#notice').className = `notice ${type}`; }
function refreshButtons() {
  $('#fetch-records').disabled = state.busy || state.fetching;
  $('#fetch-records').innerHTML = state.fetching ? '正在读取 DNS…' : '读取 DNS 中的 IP <span>↗</span>';
  $('#start').disabled = state.busy || state.fetching || !state.snapshot || !state.selected.size;
  $('#start').innerHTML = state.busy ? '测速进行中…' : '开始测速 <span>→</span>';
  $('#stop').disabled = !state.busy || !state.job || state.job.state === 'stopping';
  $('#export-ips').disabled = !state.selected.size;
  $('#export-csv').disabled = !state.job?.results?.length;
  $('#select-all').disabled = state.busy || state.fetching;
  ['#preset', '#test-url', '#rounds', '#timeout'].forEach(id => { $(id).disabled = state.busy; });
  document.querySelectorAll('input[data-ip]').forEach(input => { input.disabled = state.busy || input.dataset.eligible !== 'true'; });
}
function render() {
  const records = state.snapshot?.records || [];
  const results = state.job?.results || [];
  const byIP = new Map(results.map(row => [row.ip, row]));
  const eligible = records.filter(row => row.eligible);
  const good = results.filter(row => row.ok).sort((a, b) => b.avg_speed_MBps - a.avg_speed_MBps);
  $('#count').innerHTML = `${records.length || '—'}<small> / ${state.selected.size}</small>`;
  $('#success').textContent = results.length ? `${good.length} / ${state.job.record_count}` : '—';
  $('#fastest').innerHTML = `${good.length ? speed(good[0].avg_speed_MBps) : '—'}<small> MB/s</small>`;
  $('#winner').textContent = good.length ? good[0].ip : '等待你的第一轮测速';
  $('#list-tag').textContent = state.snapshot ? state.snapshot.domain : '等待连接';
  $('#empty').hidden = !!records.length;
  $('#selection').hidden = !records.length;
  $('#table-wrap').hidden = !records.length;
  $('#select-all').checked = eligible.length > 0 && eligible.every(row => state.selected.has(row.ip));
  $('#select-all').indeterminate = state.selected.size > 0 && !$('#select-all').checked;
  const sorted = [...records].sort((a, b) => (byIP.get(b.ip)?.avg_speed_MBps ?? -1) - (byIP.get(a.ip)?.avg_speed_MBps ?? -1));
  $('#tbody').innerHTML = sorted.map(record => {
    const row = byIP.get(record.ip);
    const active = state.busy && state.job?.current_ip === record.ip;
    const short = row?.runs?.some(run => run.ok && run.small_sample);
    const partial = row?.runs?.some(run => run.ok && run.partial);
    const roundText = row?.runs?.map(run => run.ok ? `${speed(run.speed_MBps)} MB/s` : run.kind === 'rate_limited' ? '429 限流' : '失败').join(' / ');
    const score = row?.ok ? `<span class="speed">${speed(row.avg_speed_MBps)} <small>MB/s</small></span><span class="sub mono">${esc(roundText)}</span>` : `<span class="sub">${row ? '无有效样本' : '—'}</span>`;
    let badge = active ? '<span class="tag warn">正在下载</span>' : '<span class="tag">等待测试</span>';
    if (!active && !row && !state.selected.has(record.ip)) badge = '<span class="tag">未选择</span>';
    if (!active && row) badge = `<span class="tag ${row.ok ? (short || row.successful_runs < row.expected_runs ? 'warn' : 'ok') : 'error'}">${row.successful_runs}/${row.expected_runs} 有效${short ? ' · 短样本' : partial ? ' · 限时采样' : ''}</span>`;
    if (!record.eligible) badge = '<span class="tag">非 Cloudflare 公网 IP</span>';
    const error = row?.error ? `<span class="sub source-error">${esc(row.error)}</span>` : '';
    const extra = row ? `<span class="sub">${esc(row.statuses || '')}${row.colo ? ` · ${esc(row.colo)}` : ''}</span>` : '';
    return `<tr class="${!state.selected.has(record.ip) ? 'unselected' : ''}"><td><input type="checkbox" aria-label="选择 ${esc(record.ip)}" data-ip="${esc(record.ip)}" data-eligible="${record.eligible}" ${state.selected.has(record.ip) ? 'checked' : ''} ${state.busy || !record.eligible ? 'disabled' : ''}></td><td><span class="ip">${esc(record.ip)}</span><span class="sub">${esc(record.type)} · ${record.proxied ? '橙云代理 · 记录内容' : 'DNS only'}</span></td><td>${score}</td><td>${row?.ok ? `${Math.round(row.avg_first_byte_ms)} ms` : '—'}</td><td>${badge}${extra}${error}</td></tr>`;
  }).join('');
  $('#progress').hidden = !state.job;
  if (state.job) {
    const job = state.job;
    $('#progress-bar').max = job.total; $('#progress-bar').value = job.completed;
    $('#progress-count').textContent = `${job.completed} / ${job.total}`;
    $('#progress-label').textContent = state.busy ? `${job.current_ip || '准备连接'} · 第 ${job.current_round} 轮` : `已下载 ${(job.bytes / 1e6).toFixed(1)} MB · ${new Date((job.finished || job.created) * 1000).toLocaleString()}`;
  }
  let bytes = 0;
  try { bytes = Number(new URL($('#test-url').value).searchParams.get('bytes')) || 0; } catch {}
  $('#traffic').textContent = bytes > 0 ? `预计下载最多 ${(bytes * state.selected.size * Number($('#rounds').value) / 1e6).toFixed(0)} MB；逐个 IP 测试。` : '逐个 IP 下载，每次最多 100 MB，以单次时限为准。';
  refreshButtons();
}
$('#dns-form').addEventListener('submit', async event => {
  event.preventDefault(); if (state.busy || state.fetching) return;
  state.fetching = true; refreshButtons(); notice('正在查询 Cloudflare DNS，请稍候…');
  try {
    const data = await api('/api/records', {token: $('#token').value, domain: $('#domain').value, zone_id: $('#zone-id').value});
    $('#token').value = '';
    state.snapshot = data; state.selected = new Set(data.records.filter(row => row.eligible).map(row => row.ip)); state.job = null;
    const proxied = data.records.some(row => row.proxied);
    const excluded = data.records.filter(row => !row.eligible).length;
    notice(`已读取 ${data.records.length} 个 IP。${proxied ? '含橙云记录：测试的是配置中的记录内容。' : ''}${excluded ? `${excluded} 个非 Cloudflare 公网 IP 已跳过。` : ''}${data.truncated ? '记录超过上限，本次仅载入前 100 条。' : ''}勾选后开始下载测速。`, proxied || excluded || data.truncated ? 'warn' : 'success');
  } catch (error) { notice(error.message, 'error'); }
  finally { state.fetching = false; render(); }
});
$('#tbody').addEventListener('change', event => {
  const input = event.target.closest('input[data-ip]'); if (!input || state.busy) return;
  if (input.checked) state.selected.add(input.dataset.ip); else state.selected.delete(input.dataset.ip);
  render();
});
$('#select-all').addEventListener('change', event => {
  state.selected = new Set(event.target.checked ? state.snapshot.records.filter(row => row.eligible).map(row => row.ip) : []); render();
});
$('#preset').addEventListener('change', () => {
  if ($('#preset').value === 'official') $('#test-url').value = 'https://speed.cloudflare.com/__down?bytes=10000000'; else $('#test-url').focus();
  render();
});
$('#test-url').addEventListener('input', () => { $('#preset').value = $('#test-url').value === 'https://speed.cloudflare.com/__down?bytes=10000000' ? 'official' : 'custom'; render(); });
$('#rounds').addEventListener('change', render);
$('#test-form').addEventListener('submit', async event => {
  event.preventDefault(); if (state.busy || !state.selected.size) return;
  try {
    state.busy = true; refreshButtons(); notice('逐个下载测速中；可随时停止，已完成的结果会保留。');
    state.job = await api('/api/jobs', {snapshot_id: state.snapshot.snapshot_id, ips: [...state.selected], test_url: $('#test-url').value, rounds: Number($('#rounds').value), timeout: Number($('#timeout').value)});
    render(); await pollJob(state.job.id);
  } catch (error) { notice(error.message, 'error'); state.busy = false; render(); }
});
async function pollJob(id) {
  state.pollFailures = 0;
  while (state.busy && state.job?.id === id) {
    await wait(650);
    try { state.job = await api(`/api/jobs/${id}`); state.pollFailures = 0; }
    catch (error) {
      if (error.message.includes('找不到')) { state.busy = false; notice('任务已过期或本地服务已重启，请重新读取 DNS。', 'error'); render(); return; }
      state.pollFailures++;
      notice(state.pollFailures < 5 ? '暂时无法获取进度，正在重试…' : '与本地服务的连接中断；后台任务可能仍在运行，正在自动重连。', 'warn');
      await wait(2000); continue;
    }
    const job = state.job;
    if (!['running','stopping'].includes(job.state)) {
      state.busy = false;
      if (job.state === 'rate_limited') notice(job.message, 'warn');
      else if (job.state === 'failed') notice(job.message, 'error');
      else if (job.state === 'cancelled') notice('已停止测试，已完成的结果可以导出。', 'warn');
      else notice('测速完成，已按有效轮次的平均速度排序。短样本和失败轮次已单独标记。', 'success');
    } else notice(job.state === 'stopping' ? '正在停止当前下载…' : `正在测试 ${job.current_ip || '候选 IP'}，第 ${job.current_round} 轮。`);
    render();
  }
}
$('#stop').addEventListener('click', async () => {
  if (!state.job || !state.busy) return;
  try { state.job = await api(`/api/jobs/${state.job.id}/cancel`, {}); notice('正在停止当前下载…', 'warn'); render(); }
  catch (error) { notice(error.message, 'error'); }
});
function download(name, text, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const link = document.createElement('a'); link.href = url; link.download = name; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$('#export-ips').addEventListener('click', () => {
  const rows = [...(state.snapshot?.records || [])].sort((a,b) => (state.job?.results?.find(row=>row.ip===b.ip)?.avg_speed_MBps ?? -1) - (state.job?.results?.find(row=>row.ip===a.ip)?.avg_speed_MBps ?? -1));
  download(`${state.snapshot.domain}-ips.txt`, rows.filter(row => state.selected.has(row.ip)).map(row => row.ip).join('\r\n') + '\r\n', 'text/plain;charset=utf-8');
});
$('#export-csv').addEventListener('click', () => {
  const cell = value => `"${String(value ?? '').replace(/"/g,'""')}"`;
  const header = ['IP','类型','平均下载速度(MB/s)','最慢有效轮次(MB/s)','最快有效轮次(MB/s)','首字节(ms)','有效轮次','测试轮次','HTTP','机房','错误','测速URL','测试时间'];
  const rows = state.job.results.map(row => [row.ip,row.type,row.avg_speed_MBps,row.min_speed_MBps,row.max_speed_MBps,row.avg_first_byte_ms,row.successful_runs,row.expected_runs,row.statuses,row.colo,row.error,state.job.test_url,new Date(state.job.created * 1000).toISOString()]);
  download(`${state.snapshot.domain}-results.csv`, '\ufeff' + [header,...rows].map(row=>row.map(cell).join(',')).join('\r\n'), 'text/csv;charset=utf-8');
});
render();
