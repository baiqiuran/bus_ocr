'use strict';
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {scanning:'扫描中',ready:'待识别',running:'识别中',partial:'部分完成',completed:'处理完成',failed:'运行失败',interrupted:'已中断',importing:'导入中'};
let token = '', selectedJob = '', page = 1, pages = 1, jobData = null, jobs = [], importMode = false, recordVersion = 0, refreshing = false;

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':token},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}
function notice(message, success = false) {
  $('notice').textContent = message;
  $('notice').classList.toggle('success', success);
  $('notice').hidden = !message;
}
function number(value) { return Number(value || 0).toLocaleString('zh-CN'); }
function setText(id, text) { $(id).textContent = text; }
function openForm(mode) {
  importMode = mode;
  setText('form-title', mode ? '导入结果' : '新建批次');
  setText('form-copy', mode ? '选择已有的端点识别输出目录，重复导入会打开原批次。' : '扫描目录及子目录中的 JPG / JPEG 图片。');
  setText('submit-job', mode ? '导入结果 →' : '扫描目录 →');
  $('source').value = '';
  $('source').placeholder = mode ? 'E:\\bus_ocr\\output\\endpoints' : 'E:\\公交站牌\\安平小区';
  $('source').removeAttribute('aria-invalid');
  $('mode-note').hidden = mode;
  setText('form-error', '');
  $('create-dialog').showModal();
  $('source').focus();
}
function drawJobs() {
  const jobMarkup = jobs.length ? jobs.map(job => `<button class="job-button ${selectedJob === job.id ? 'selected' : ''}" data-job="${escapeHTML(job.id)}" title="${escapeHTML(job.name)}"><strong>${escapeHTML(job.name)}</strong><small>${escapeHTML(labels[job.status] || job.status)}${job.imported ? ' · 历史导入' : ''}</small></button>`).join('') : '<p class="muted">还没有识别批次</p>';
  if ($('jobs')._markup !== jobMarkup) { $('jobs').innerHTML = jobMarkup; $('jobs')._markup = jobMarkup; }
  $('all-view').classList.toggle('active', !selectedJob && !$('review-filter').value);
  $('review-view').classList.toggle('active', !selectedJob && $('review-filter').value === '1');
}
async function loadOverview() {
  const data = await api('/api/jobs');
  jobs = data.jobs;
  for (const name of ['records','stations','routes','review']) setText(`stat-${name}`, number(data.stats[name]));
  setText('nav-count', number(data.stats.records)); setText('review-count', number(data.stats.review));
  setText('database', data.database);
  drawJobs();
}
async function selectJob(id, review = '') {
  selectedJob = id; page = 1; jobData = null;
  $('review-filter').value = review;
  $('search').value = ''; $('direction').value = '';
  $('batch-panel').hidden = !id; $('export').hidden = !id; $('stats').hidden = Boolean(id);
  setText('page-title', id ? (jobs.find(job => job.id === id)?.name || '识别批次') : review ? '待复核记录' : '站牌数据');
  drawJobs();
  await Promise.all([loadRecords(), loadBatch()]);
}
async function loadBatch() {
  if (!selectedJob) return;
  const id = selectedJob;
  const data = await api(`/api/jobs/${id}`);
  if (id !== selectedJob) return;
  jobData = data;
  setText('batch-name', '处理进度'); setText('batch-path', data.source);
  setText('batch-status', `${labels[data.status] || data.status}${data.imported ? ' · 历史导入' : ''}`);
  $('batch-status').className = `badge ${['running','scanning'].includes(data.status) ? 'running' : ''}`;
  const counts = data.counts;
  $('batch-counts').innerHTML = [['total','图片'],['done','完成'],['failed','失败'],['skipped','无线路信息'],['incomplete','未完成']].map(([key,label]) => `<span>${label}<b>${number(counts[key])}</b></span>`).join('');
  const active = ['running','scanning'].includes(data.status);
  const busy = jobs.some(job => ['running','scanning'].includes(job.status));
  if (data.status === 'scanning') $('progress').removeAttribute('value');
  else $('progress').value = counts.total ? (counts.done + counts.failed + counts.skipped) / counts.total * 100 : 0;
  const messages = {scanning:'正在扫描图片…',ready:'建议先抽样核对，再识别整批。',running:'识别中，结果将在本轮结束后入库。',completed:'',partial:'结果已保存，可继续处理剩余图片。',interrupted:'已中断，可从上次进度继续。',failed:'请查看运行错误，修正环境或输入后继续。'};
  setText('batch-message', data.imported ? '历史批次 · 只读' : counts.total === 0 && data.status === 'ready' ? '目录中没有 JPG / JPEG 图片。请新建批次并选择正确目录。' : messages[data.status] || '');
  $('sample').hidden = Boolean(data.imported) || data.status !== 'ready';
  $('start').hidden = Boolean(data.imported) || data.status === 'completed';
  $('batch-message').hidden = !$('batch-message').textContent;
  $('progress').hidden = !active;
  $('sample').disabled = active || busy || !counts.total || data.status !== 'ready';
  $('start').disabled = active || busy || !counts.total || data.status === 'completed';
  setText('start', data.status === 'running' ? '识别进行中…' : ['partial','failed','interrupted'].includes(data.status) ? '继续识别 →' : '开始整批识别 →');
  $('batch-error').hidden = !data.error; setText('batch-error', data.error);
}
async function loadRecords() {
  const version = ++recordVersion;
  const query = new URLSearchParams({job:selectedJob, page, q:$('search').value.trim(), direction:$('direction').value, review:$('review-filter').value});
  const data = await api(`/api/records?${query}`);
  if (version !== recordVersion) return;
  pages = data.pages;
  setText('result-count', number(data.total));
  const rowMarkup = data.rows.map(row => `<tr><td data-label="线路"><span class="route-tag">${escapeHTML(row.route || '待确认')}</span></td><td data-label="站点"><span class="station-name">${escapeHTML(row.station || '待确认')}</span><span class="direction-tag">${escapeHTML(row.direction || '未知')}</span></td><td data-label="首末站"><div class="route-pair"><span><span class="endpoint-label">起</span>${escapeHTML(row.first_stop || '首站待确认')}</span><span><span class="endpoint-label">终</span>${escapeHTML(row.last_stop || '末站待确认')}</span></div></td><td data-label="来源"><div class="source-name">${escapeHTML(row.poster)}</div></td><td data-label="状态"><span class="badge ${row.needs_review ? 'review' : ''}">${row.needs_review ? '◎ 待复核' : '已提取'}</span></td><td><button class="detail-button" data-record="${row.id}" aria-label="查看 ${escapeHTML(row.station)} ${escapeHTML(row.route)} 的来源">详情 ↗</button></td></tr>`).join('');
  if ($('rows')._markup !== rowMarkup) { $('rows').innerHTML = rowMarkup; $('rows')._markup = rowMarkup; }
  $('empty').hidden = data.rows.length > 0;
  const filtered = Boolean($('search').value || $('direction').value || $('review-filter').value);
  setText('empty-title', filtered ? '没有符合条件的记录' : selectedJob ? '这个批次还没有线路结果' : '还没有站牌记录');
  setText('empty-copy', filtered ? '换一个关键词或清除筛选，查看其他站牌。' : selectedJob ? '扫描后开始识别；本轮处理结束，线路结果会自动入库。' : '新建批次，或导入已有结果。');
  setText('empty-action', filtered ? '清除筛选' : '新建识别批次');
  $('empty-action').hidden = Boolean(selectedJob && !filtered);
  setText('page-info', `共 ${number(data.total)} 条 · 第 ${data.page} / ${data.pages} 页`);
  $('prev').disabled = page <= 1; $('next').disabled = page >= pages;
  drawJobs();
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try { await loadOverview(); await Promise.all([loadBatch(),loadRecords()]); }
  finally { refreshing = false; }
}
async function showRecord(id) {
  const row = await api(`/api/records/${id}`);
  setText('detail-title', `${row.station || '待确认站点'} · ${row.route || '未知线路'}`);
  const fields = [['站点',row.station],['方位',row.direction],['首站',row.first_stop],['末站',row.last_stop]];
  const sourceFields = [['图片',row.poster],['板块',row.block_index],['站点键',row.station_key],['原图路径',row.image_path || '来源不可用'],['图片哈希',row.image_hash || '未关联'],['输出目录',row.output]];
  const fieldList = items => items.map(([key,value])=>`<dt>${key}</dt><dd>${escapeHTML(value === '' ? '—' : value)}</dd>`).join('');
  $('detail-content').innerHTML = `<div class="detail-layout"><div class="photo-pane">${row.image_path ? `<img id="source-image" src="/api/image/${row.id}" alt="${escapeHTML(row.poster)} 原始站牌" width="800" height="1000">` : '<p>原图来源不唯一或不可用</p>'}</div><div class="detail-data"><div class="detail-actions"><span class="badge ${row.needs_review ? 'review' : ''}">${row.needs_review ? '◎ 待复核' : '已提取'}</span>${row.image_path ? `<a class="original-link" href="/api/image/${row.id}" target="_blank" rel="noopener">打开原图 ↗</a>` : ''}</div><dl class="primary-fields">${fieldList(fields)}</dl><div class="evidence"><strong>复核</strong>${row.reviews.length ? row.reviews.map(item=>`<div class="review-evidence"><b>${escapeHTML(item.reason)}</b> · ${escapeHTML(item.status)}<br>${escapeHTML(item.detail || item.ocr_text)}${item.crop_path ? `<details><summary>裁片路径</summary><p class="path">${escapeHTML(item.crop_path)}</p></details>` : ''}</div>`).join('') : `<p>${row.needs_review ? '字段不完整或存在疑点，请核对原图。' : '无复核标记，未经人工确认。'}</p>`}</div><details class="source-meta"><summary>来源信息</summary><dl>${fieldList(sourceFields)}</dl></details></div></div>`;
  const image = $('source-image');
  if (image) image.addEventListener('error',()=>{image.parentElement.textContent='原图不可访问，可能已经移动。可根据右侧图片路径核对。';});
  $('detail-dialog').showModal();
}
function showIssues() {
  if (!jobData) return;
  const anomalies = jobData.posters.filter(item=>item.status !== 'done');
  const report = jobData.report;
  $('issues-content').innerHTML = `<p class="path">${escapeHTML(jobData.output)}</p><p>本轮远程 OCR 调用 ${number(report.ocr?.remote_calls)} · AI 复核调用 ${number(report.vlm?.calls)}</p><h3>未完成、失败与跳过 · ${anomalies.length}</h3>${anomalies.length ? anomalies.map(item=>`<div class="issue-item"><strong>${escapeHTML(item.path)}</strong><p>${escapeHTML(item.status)} · ${escapeHTML(item.error || '尚未处理完成')}</p></div>`).join('') : '<p class="muted">没有此类图片记录；完整统计以批次报告为准。</p>'}<h3>复核项 · ${jobData.reviews.length}</h3>${jobData.reviews.length ? jobData.reviews.map(item=>`<div class="issue-item"><strong>${escapeHTML(item.route || '未知线路')} · ${escapeHTML(item.reason)}</strong><p>${escapeHTML(item.status)} · ${escapeHTML(item.detail || item.ocr_text)}</p><p>${escapeHTML(item.crop_path)}</p></div>`).join('') : '<p class="muted">没有关联的复核项。</p>'}`;
  $('issues-dialog').showModal();
}
function handle(action) { return (...args) => Promise.resolve().then(()=>action(...args)).catch(error=>notice(error.message)); }
$('new-job').onclick = () => openForm(false);
$('menu-toggle').onclick = () => { const rail = document.querySelector('.rail'); const expanded = rail.classList.toggle('expanded'); $('menu-toggle').setAttribute('aria-expanded',String(expanded)); };
$('import-job').onclick = () => openForm(true);
document.querySelectorAll('.close').forEach(button=>button.onclick=()=>button.closest('dialog').close());
document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click', event=>{if(event.target===dialog){const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();}}));
$('create-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('submit-job').disabled = true; setText('form-error','');
  setText('submit-job', importMode ? '正在导入…' : '正在创建…');
  try {
    const data = await api(importMode ? '/api/import' : '/api/jobs', {path:$('source').value.trim().replace(/^"|"$/g,'')});
    $('create-dialog').close(); notice('');
    await loadOverview(); await selectJob(data.id);
  } catch(error) { setText('form-error',error.message); $('source').setAttribute('aria-invalid','true'); }
  finally { $('submit-job').disabled = false; setText('submit-job',importMode ? '导入结果 →' : '扫描目录 →'); }
});
$('source').addEventListener('input',()=>{$('source').removeAttribute('aria-invalid');setText('form-error','');});
$('jobs').onclick = handle(event=>{const button=event.target.closest('[data-job]');if(button)return selectJob(button.dataset.job);});
$('all-view').onclick = handle(()=>selectJob(''));
$('review-view').onclick = handle(()=>selectJob('','1'));
$('refresh').onclick = handle(refresh);
$('rows').onclick = handle(event=>{const button=event.target.closest('[data-record]');if(button)return showRecord(button.dataset.record);});
$('issues').onclick = showIssues;
for (const id of ['sample','start']) $(id).onclick = handle(async()=>{
  $('sample').disabled = $('start').disabled = true;
  try { await api('/api/start',{id:selectedJob,sample:id==='sample'});notice(''); }
  finally { await refresh(); }
});
$('export').onclick = ()=>{if(selectedJob)window.location.href=`/api/export/${selectedJob}`;};
let searchTimer;
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(handle(()=>{page=1;return loadRecords();}),220);});
for(const id of ['direction','review-filter']) $(id).onchange=handle(()=>{page=1;return loadRecords();});
$('prev').onclick=handle(()=>{page=Math.max(1,page-1);return loadRecords();});
$('next').onclick=handle(()=>{page=Math.min(pages,page+1);return loadRecords();});
$('empty-action').onclick=handle(()=>{if($('search').value||$('direction').value||$('review-filter').value){$('search').value='';$('direction').value='';$('review-filter').value='';page=1;return loadRecords();}openForm(false);});
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'&&!document.querySelector('dialog[open]')){event.preventDefault();$('search').focus();}});
async function boot(){token=(await api('/api/session')).token;await refresh();setInterval(()=>{if(!document.hidden)refresh().catch(error=>notice(`连接暂时中断：${error.message}`));},4000);}
boot().catch(error=>notice(`无法连接本地服务：${error.message}。请检查服务后刷新页面。`));
