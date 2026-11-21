"""Live dashboard for the voice logistics agent. ONE file, read-only, touches none of your other code.

    python dashboard.py            ->  open http://localhost:8000
    python dashboard.py --port 9000

Put it in the project folder (next to logistics.db). It reads the same DB_PATH / CALLS_DIR as the agent
(.env is honoured if python-dotenv is installed) and never writes anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", HERE / "logistics.db"))
CALLS_DIR = Path(os.getenv("CALLS_DIR", HERE / "calls"))


def q(sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def ops_events(n: int = 100) -> list[dict]:
    f = CALLS_DIR / "ops_notifications.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out[::-1]


def all_data() -> dict:
    return {
        "calls": q("""SELECT c.id, c.call_code, c.phone_number, c.call_type, c.language, c.summary, c.urgency,
                      c.escalated, c.outcome, c.started_at, c.ended_at, cu.company_name
                      FROM calls c LEFT JOIN customers cu ON cu.id = c.customer_id
                      ORDER BY c.id DESC LIMIT 500"""),
        "requests": q("""SELECT r.*, cu.company_name, cu.email FROM transport_requests r
                         LEFT JOIN customers cu ON cu.id = r.customer_id ORDER BY r.id DESC LIMIT 500"""),
        "shipments": q("SELECT * FROM shipments ORDER BY id DESC LIMIT 500"),
        "customers": q("SELECT * FROM customers ORDER BY id DESC LIMIT 500"),
        "ops": ops_events(),
        "db": str(DB_PATH),
    }


def one_call(cid: int) -> dict:
    rows = q("""SELECT c.*, cu.company_name FROM calls c LEFT JOIN customers cu ON cu.id = c.customer_id
                WHERE c.id = ?""", (cid,))
    if not rows:
        return {}
    r = rows[0]
    try:
        r["transcript"] = json.loads(r.get("transcript") or "[]")
    except ValueError:
        r["transcript"] = []
    return r


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/":
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/data":
            self._send(json.dumps(all_data(), default=str).encode(), "application/json")
        elif path.startswith("/api/call/") and path[10:].isdigit():
            self._send(json.dumps(one_call(int(path[10:])), default=str).encode(), "application/json")
        else:
            self._send(b"not found", "text/plain", 404)

    def log_message(self, *a) -> None:  # keep the console quiet
        pass


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logistics Agent Dashboard</title>
<style>
:root{--ink:#18232f;--mute:#5f6b78;--bg:#f2f5f7;--card:#fff;--line:#d9e0e6;--acc:#0f766e;--soft:#dff3f0;--bad:#b42318;--badsoft:#fde8e6;--warn:#b45309;--warnsoft:#fdf0dd}
@media(prefers-color-scheme:dark){:root{--ink:#e6edf3;--mute:#93a1ae;--bg:#0f161d;--card:#17212b;--line:#27333f;--acc:#3cc4b7;--soft:#12332f;--bad:#f87a70;--badsoft:#3a1816;--warn:#f0a850;--warnsoft:#3a2a12}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 "Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;align-items:center;gap:16px;padding:14px 28px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;flex-wrap:wrap}
header b{font-size:16px}header small{color:var(--mute)}
nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap}
nav button{border:0;background:none;color:var(--mute);padding:7px 13px;border-radius:8px;cursor:pointer;font:inherit}
nav button.on{background:var(--soft);color:var(--acc);font-weight:600}nav button:hover{color:var(--ink)}
main{padding:24px 28px 60px;max-width:1280px;margin:auto}
.grid{display:grid;gap:14px}.k{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}.two{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;min-width:0}
.kpi b{display:block;font-size:28px;line-height:1.15}.kpi span{color:var(--mute);font-size:13px}
h3{margin:0 0 12px;font-size:14px}
.bar{display:grid;grid-template-columns:110px 1fr 36px;gap:10px;align-items:center;margin:7px 0;font-size:13px}
.bar i{display:block;height:9px;border-radius:5px;background:var(--acc)}.bar div{background:var(--bg);border-radius:5px}
.tools{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}
input,select{font:inherit;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}input{min-width:240px}
.tw{overflow:auto;border:1px solid var(--line);border-radius:12px;background:var(--card)}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;padding:10px 12px;color:var(--mute);font-weight:600;border-bottom:1px solid var(--line);white-space:nowrap;background:var(--card);position:sticky;top:0}
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border:0}
tr.cl{cursor:pointer}tr.cl:hover td{background:var(--bg)}
.sum{max-width:420px;color:var(--mute)}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;background:var(--soft);color:var(--acc);white-space:nowrap}
.pill.high,.pill.delayed{background:var(--badsoft);color:var(--bad)}.pill.normal,.pill.in_transit,.pill.new{background:var(--warnsoft);color:var(--warn)}
.empty{padding:34px;text-align:center;color:var(--mute)}
#drawer{position:fixed;inset:0 0 0 auto;width:min(560px,100%);background:var(--card);border-left:1px solid var(--line);transform:translateX(105%);transition:transform .2s;z-index:20;display:flex;flex-direction:column;box-shadow:-8px 0 30px rgba(0,0,0,.15)}
#drawer.open{transform:none}#dh{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px}
#db{padding:16px 20px;overflow:auto;flex:1}.x{border:0;background:none;font-size:22px;cursor:pointer;color:var(--mute)}
.meta{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:10px 0 16px;font-size:13px}.meta span{color:var(--mute)}
.msg{margin:8px 0;max-width:86%;padding:8px 12px;border-radius:12px;background:var(--bg)}.msg small{display:block;color:var(--mute);font-size:11.5px}
.msg.caller{margin-left:auto;background:var(--soft)}.msg.ivr{background:none;border:1px dashed var(--line);color:var(--mute)}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<header><b>Logistics Agent</b><small id="stamp">loading…</small><nav id="nav"></nav></header>
<main id="main"></main>
<aside id="drawer"><div id="dh"><div id="dt"></div><button class="x" onclick="closeD()" aria-label="Close">×</button></div><div id="db"></div></aside>
<script>
const $=s=>document.querySelector(s);
const E=v=>String(v??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const TABS=[["overview","Overview"],["calls","Calls"],["requests","Requests"],["shipments","Shipments"],["customers","Customers"],["ops","Ops alerts"]];
let D=null,tab=(location.hash.slice(1)||"overview");
const pill=v=>v?`<span class="pill ${E(v)}">${E(v)}</span>`:"";
const dt=v=>v?E(String(v).replace("T"," ").slice(0,16)):"";
const dur=(a,b)=>{if(!a||!b)return"";const s=(new Date(b)-new Date(a))/1000;return isNaN(s)||s<0?"":s>=60?Math.floor(s/60)+"m "+Math.round(s%60)+"s":Math.round(s)+"s"};
const count=(arr,k)=>arr.reduce((m,r)=>{const v=r[k]||"unknown";m[v]=(m[v]||0)+1;return m},{});
const bars=o=>{const e=Object.entries(o).sort((a,b)=>b[1]-a[1]);const m=Math.max(1,...e.map(x=>x[1]));return e.length?e.map(([k,v])=>`<div class="bar"><span>${E(k)}</span><div><i style="width:${v/m*100}%"></i></div><b>${v}</b></div>`).join(""):'<div class="empty">No data yet</div>'};
const table=(cols,rows,rowAttr)=>rows.length?`<div class="tw"><table><thead><tr>${cols.map(c=>`<th>${c[0]}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr ${rowAttr?rowAttr(r):""}>${cols.map(c=>`<td>${c[1](r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`:'<div class="card empty">Nothing here yet</div>';

function overview(){
  const c=D.calls,esc=c.filter(x=>x.escalated).length,hi=c.filter(x=>x.urgency==="high").length;
  const today=new Date().toISOString().slice(0,10),td=c.filter(x=>(x.started_at||"").startsWith(today)).length;
  const secs=c.map(x=>(new Date(x.ended_at)-new Date(x.started_at))/1000).filter(s=>s>0&&s<7200),avg=secs.length?Math.round(secs.reduce((a,b)=>a+b,0)/secs.length):0;
  const k=(n,l)=>`<div class="card kpi"><b>${n}</b><span>${l}</span></div>`;
  return `<div class="grid k">${k(c.length,"Calls")}${k(td,"Calls today")}${k(D.requests.length,"Transport requests")}${k(D.shipments.length,"Shipments")}${k(D.customers.length,"Customers")}${k(hi,"High-urgency calls")}${k(esc,"Escalated to a person")}${k(avg?Math.floor(avg/60)+"m "+avg%60+"s":"–","Average call length")}</div>
  <div class="grid two" style="margin-top:14px"><div class="card"><h3>Calls by type</h3>${bars(count(c,"call_type"))}</div><div class="card"><h3>Calls by language</h3>${bars(count(c,"language"))}</div>
  <div class="card"><h3>Call outcomes</h3>${bars(count(c,"outcome"))}</div><div class="card"><h3>Shipments by status</h3>${bars(count(D.shipments,"status"))}</div></div>
  <h3 style="margin:22px 0 10px">Latest calls</h3>${callTable(c.slice(0,6))}`;
}
const callTable=rows=>table([["Call",r=>E(r.call_code)],["Started",r=>dt(r.started_at)],["Length",r=>dur(r.started_at,r.ended_at)],["Caller",r=>E(r.company_name||r.phone_number)],["Type",r=>E(r.call_type)],["Lang",r=>E((r.language||"").toUpperCase())],["Urgency",r=>pill(r.urgency)],["Outcome",r=>E(r.outcome)],["Summary",r=>`<div class="sum">${E(r.summary)}</div>`]],rows,r=>`class="cl" onclick="openCall(${r.id})"`);
function calls(){return `<div class="tools"><input id="cq" placeholder="Search summary, phone, company…" oninput="fillCalls()"><select id="ct" onchange="fillCalls()"><option value="">All types</option><option>track</option><option>request</option><option>issue</option></select><select id="cu" onchange="fillCalls()"><option value="">All urgency</option><option>high</option><option>normal</option><option>low</option></select></div><div id="cl"></div>`}
function fillCalls(){const s=($("#cq")?.value||"").toLowerCase(),t=$("#ct")?.value||"",u=$("#cu")?.value||"";
  $("#cl").innerHTML=callTable(D.calls.filter(r=>(!t||r.call_type===t)&&(!u||r.urgency===u)&&(!s||[r.summary,r.phone_number,r.company_name,r.call_code,r.outcome].join(" ").toLowerCase().includes(s))))}
const yn=v=>v?"yes":"no";
function requests(){return table([["Request",r=>"REQ-"+String(r.id).padStart(4,"0")],["Created",r=>dt(r.created_at)],["Customer",r=>E(r.company_name)],["Route",r=>E(r.pickup_location)+" → "+E(r.delivery_location)],["Cargo",r=>E(r.cargo_type)],["kg",r=>E(r.weight_kg)],["Temp",r=>r.temperature_controlled?E(r.temperature_min_c)+" to "+E(r.temperature_max_c)+" °C":"–"],["ADR",r=>r.adr?"class "+E(r.adr_class):"no"],["Pickup",r=>E(r.pickup_date)],["Delivery",r=>E(r.delivery_date)],["Email",r=>E(r.email)],["Notes",r=>E(r.special_requirements)],["Status",r=>pill(r.status)]],D.requests)}
function shipments(){return table([["Tracking",r=>E(r.tracking_number)],["Status",r=>pill(r.status)],["Route",r=>E(r.pickup_location)+" → "+E(r.delivery_location)],["Now at",r=>E(r.current_location)],["Cargo",r=>E(r.cargo_type)],["kg",r=>E(r.weight_kg)],["ETA",r=>dt(r.eta_start)+(r.eta_end?" – "+dt(r.eta_end):"")],["Driver",r=>E(r.driver_name)+" "+E(r.driver_phone)],["Issue",r=>r.issue_status==="reported"?`<span class="pill high">${E(r.issue_text)}</span>`:"–"]],D.shipments)}
function customers(){return table([["ID",r=>r.id],["Company",r=>E(r.company_name)],["Contact",r=>E(r.contact_name)],["Phone",r=>E(r.phone)],["Email",r=>E(r.email)],["Country",r=>E(r.country)],["Language",r=>E(r.preferred_language)]],D.customers)}
function ops(){return table([["Time",r=>dt(r.time)],["Event",r=>pill(r.event)],["Details",r=>E(Object.entries(r).filter(([k])=>!["time","event"].includes(k)).map(([k,v])=>k+": "+v).join(" · "))]],D.ops)}
const VIEW={overview,calls,requests,shipments,customers,ops};

function draw(){
  $("#nav").innerHTML=TABS.map(t=>`<button class="${t[0]===tab?"on":""}" onclick="go('${t[0]}')">${t[1]}</button>`).join("");
  if(!D)return;
  if(tab==="calls"&&$("#cl")){fillCalls()}else{$("#main").innerHTML=(VIEW[tab]||overview)();if(tab==="calls")fillCalls()}
}
function go(t){tab=t;history.replaceState(null,"","#"+t);$("#main").innerHTML="";draw()}
async function load(){try{D=await (await fetch("/api/data")).json();$("#stamp").textContent="Updated "+new Date().toLocaleTimeString()+" · "+D.db;draw()}catch(e){$("#stamp").textContent="Cannot reach the dashboard server"}}
async function openCall(id){
  const c=await (await fetch("/api/call/"+id)).json();
  $("#dt").innerHTML=`<b>${E(c.call_code)}</b> <span class="pill ${E(c.urgency)}">${E(c.urgency)}</span><div style="color:var(--mute)">${E(c.company_name||c.phone_number)}</div>`;
  $("#db").innerHTML=`<div class="meta"><span>Started</span><b>${dt(c.started_at)}</b><span>Length</span><b>${dur(c.started_at,c.ended_at)||"–"}</b><span>Type</span><b>${E(c.call_type)}</b><span>Language</span><b>${E((c.language||"").toUpperCase())}</b><span>Outcome</span><b>${E(c.outcome)}</b><span>Escalated</span><b>${yn(c.escalated)}</b></div>
  <h3>Summary</h3><p>${E(c.summary)||"<i>No summary</i>"}</p><h3 style="margin-top:18px">Transcript</h3>`+
  ((c.transcript||[]).length?c.transcript.map(m=>`<div class="msg ${E(m.role)}"><small>${E(m.role)}${m.lang?" · "+E(m.lang):""}</small>${E(m.text)}</div>`).join(""):'<div class="empty">No transcript saved</div>');
  $("#drawer").classList.add("open");
}
function closeD(){$("#drawer").classList.remove("open")}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeD()});
load();setInterval(load,10000);
</script></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    print(f"Dashboard: http://localhost:{a.port}   (database: {DB_PATH})")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()