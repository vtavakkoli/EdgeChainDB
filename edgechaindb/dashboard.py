from __future__ import annotations


def render_dashboard() -> str:
    return r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EdgeChainDB Cluster Monitor</title>
<style>
:root{--bg:#07111f;--panel:#101d31;--panel2:#152641;--text:#e8eef8;--muted:#95a5bd;--line:#263a58;--ok:#35d07f;--bad:#ff6b6b;--warn:#ffc857;--accent:#6ea8fe}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#07111f,#0d1930);color:var(--text);font:14px/1.45 Inter,Segoe UI,system-ui,sans-serif;min-height:100vh}
header{padding:25px 30px;border-bottom:1px solid var(--line);background:rgba(7,17,31,.9);position:sticky;top:0;z-index:5;backdrop-filter:blur(12px)}
h1{font-size:25px;margin:0 0 4px}.sub{color:var(--muted)}main{max-width:1500px;margin:auto;padding:24px}.toolbar,.metrics{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
button,a.button{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:9px 13px;border-radius:9px;cursor:pointer;text-decoration:none;font-weight:650}button:hover,a.button:hover{border-color:var(--accent)}button.danger{border-color:#7d3038;background:#371c25}.card{background:rgba(16,29,49,.92);border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:0 14px 35px rgba(0,0,0,.2)}
.metric{min-width:150px}.metric strong{display:block;font-size:27px}.metric span{color:var(--muted)}.grid{display:grid;grid-template-columns:2fr 1fr;gap:18px}.devices{display:grid;grid-template-columns:repeat(auto-fill,minmax(265px,1fr));gap:12px}
.device{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px}.device h3{margin:0 0 7px;display:flex;justify-content:space-between;gap:8px}.badge{font-size:11px;border-radius:20px;padding:3px 8px;background:#253854}.running{color:var(--ok)}.exited,.dead{color:var(--bad)}.paused{color:var(--warn)}.kv{display:grid;grid-template-columns:1fr 1fr;gap:5px 10px;color:var(--muted);font-size:12px}.kv b{color:var(--text);font-weight:600}.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}.actions button{font-size:11px;padding:6px 8px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);font-size:12px}th{color:var(--muted)}code{color:#b9d4ff}.error{color:var(--bad);white-space:pre-wrap}.ok{color:var(--ok)}
@media(max-width:950px){.grid{grid-template-columns:1fr}header{position:static}}
</style>
</head>
<body>
<header><h1>EdgeChainDB Cluster Monitor</h1><div class="sub">Live ledger, container state, recent events, and node controls</div></header>
<main>
<div class="toolbar">
<button onclick="allAction('start')">Start all devices</button><button onclick="allAction('stop')" class="danger">Stop all devices</button><button onclick="allAction('restart')">Restart all devices</button><button onclick="allAction('pause')">Pause all</button><button onclick="allAction('unpause')">Resume all</button><a class="button" href="/benchmark/report" target="_blank">Latest benchmark report</a><a class="button" href="/docs" target="_blank">API docs</a>
</div>
<div class="metrics" id="metrics"></div>
<div class="grid"><section class="card"><h2>Cluster machines</h2><div id="controller"></div><div class="devices" id="devices"></div></section><section class="card"><h2>Recent cluster events</h2><div id="events"></div></section></div>
</main>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,options){const r=await fetch(url,options);const text=await r.text();let data;try{data=JSON.parse(text)}catch{data=text}if(!r.ok)throw new Error(typeof data==='string'?data:JSON.stringify(data));return data}
function metric(label,value){return `<div class="card metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`}
async function load(){
 try{
  const [cluster,events]=await Promise.all([api('/cluster/state'),api('/cluster/events?limit=40')]);
  const stats=cluster.ledger;
  document.getElementById('metrics').innerHTML=metric('Docker devices',cluster.summary.running_devices+'/'+cluster.summary.total_devices)+metric('Ledger devices',stats.devices)+metric('Events',stats.events)+metric('Blocks',stats.blocks)+metric('Pending',stats.pending_events);
  document.getElementById('controller').innerHTML=cluster.controller.available?`<p class="ok">Docker control connected · project <code>${esc(cluster.controller.project)}</code></p>`:`<p class="error">Docker control unavailable: ${esc(cluster.controller.error)}</p>`;
  document.getElementById('devices').innerHTML=cluster.devices.map(d=>`<article class="device"><h3>${esc(d.service)} <span class="badge ${esc(d.state)}">${esc(d.state)}</span></h3><div class="kv"><span>Device</span><b>${esc(d.device_id)}</b><span>Sequence</span><b>${esc(d.last_sequence)}</b><span>Health</span><b>${esc(d.health||'-')}</b><span>Exit code</span><b>${esc(d.exit_code)}</b><span>Last event</span><b>${esc(d.last_event_at||'-')}</b></div><div class="actions"><button onclick="oneAction('${esc(d.service)}','start')">Start</button><button onclick="oneAction('${esc(d.service)}','stop')" class="danger">Stop</button><button onclick="oneAction('${esc(d.service)}','restart')">Restart</button><button onclick="oneAction('${esc(d.service)}','pause')">Pause</button><button onclick="oneAction('${esc(d.service)}','unpause')">Resume</button></div></article>`).join('');
  document.getElementById('events').innerHTML=`<table><thead><tr><th>Device</th><th>Seq</th><th>Type</th><th>Received</th></tr></thead><tbody>${events.map(e=>`<tr><td>${esc(e.device_id)}</td><td>${esc(e.sequence)}</td><td>${esc(e.event_type)}</td><td>${esc(e.received_at)}</td></tr>`).join('')}</tbody></table>`;
 }catch(e){document.getElementById('controller').innerHTML=`<p class="error">${esc(e)}</p>`}
}
async function oneAction(service,action){try{await api(`/cluster/devices/${service}/${action}`,{method:'POST'});setTimeout(load,400)}catch(e){alert(e)}}
async function allAction(action){try{await api(`/cluster/devices/${action}`,{method:'POST'});setTimeout(load,500)}catch(e){alert(e)}}
load();setInterval(load,3000);
</script>
</body></html>'''
