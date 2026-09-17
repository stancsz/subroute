"""Read-only presentation routes. This module never participates in model resolution."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = ROOT / "config" / "ui_sources.json"


def _sources() -> list[dict[str, object]]:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    sources = []
    for item in payload.get("sources", []):
        if not all(isinstance(item.get(key), str) for key in ("name", "kind", "models")):
            continue
        credentials = item.get("credentials", [])
        if not isinstance(credentials, list) or not all(isinstance(value, str) for value in credentials):
            credentials = []
        available = item.get("available", True) is not False
        configured = available and all(os.getenv(key) for key in credentials)
        sources.append({**item, "available": available, "configured": configured, "credentials": credentials})
    return sources


def _page() -> str:
    cards = "".join(
        f'<article class="card"><div class="top"><span class="dot {"ready" if source["configured"] else "missing"}"></span><span>{html.escape(str(source["kind"]).upper())}</span></div><h2>{html.escape(str(source["name"]))}</h2><p>{html.escape(str(source["models"]))}</p><div class="connection"><b>{"Configured" if source["configured"] else "Not added to gateway" if not source["available"] else "Credential required"}</b><span>{"gateway configuration present" if source["configured"] else "add a verified LiteLLM transport" if not source["available"] else "check runtime environment"}</span></div><div class="quota"><span>USAGE</span><strong>—</strong><small>Provider quota data unavailable</small></div></article>'
        for source in _sources()
    )
    apps = "".join(f'<li>{name}</li>' for name in ("Claude Code", "Codex", "OpenCode", "OpenClaw", "Hermes", "DeepSeek Harness"))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Subroute Sources</title><style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Manrope:wght@500;600;700;800&display=swap');:root{{color-scheme:dark;background:#10151b;color:#edf1f1;font-family:Manrope,sans-serif}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(800px 500px at 100% -15%,#203947,transparent 65%),#10151b}}main{{max-width:1120px;margin:auto;padding:52px 32px}}header{{display:flex;justify-content:space-between;gap:20px;align-items:start}}.eyebrow,.top,small{{font:500 10px 'DM Mono',monospace;letter-spacing:.12em;color:#82b9d7}}h1{{font-size:39px;letter-spacing:-.06em;margin:6px 0}}header p{{color:#8997a1;margin:0;font-size:14px}}.readonly{{border:1px solid #315443;border-radius:99px;padding:8px 10px;color:#9ddab7;font:10px 'DM Mono',monospace}}.bar{{margin:32px 0;border:1px solid #2e414d;border-radius:13px;background:#151d24;padding:15px 17px;color:#9caab3;font-size:12px}}.bar b{{color:#e9eeee}}.section{{display:flex;justify-content:space-between;align-items:end}}.section h2{{font-size:21px;margin:4px 0 0;letter-spacing:-.04em}}.section span{{font:10px 'DM Mono';color:#788791}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;margin-top:14px}}.card{{min-height:224px;padding:16px;border:1px solid #2d3943;border-radius:14px;background:linear-gradient(145deg,#19222b,#141a21)}}.top{{display:flex;justify-content:space-between}}.dot{{display:block;width:9px;height:9px;border-radius:50%}}.dot.ready{{background:#60c993;box-shadow:0 0 0 4px #1d3b31}}.dot.missing{{background:#e9b85c;box-shadow:0 0 0 4px #382f1b}}.card h2{{font-size:16px;letter-spacing:-.04em;margin:27px 0 5px}}.card p{{font-size:12px;color:#a8b4bb;margin:0;min-height:31px}}.connection{{border-top:1px solid #2d3943;margin-top:17px;padding-top:10px;display:grid;gap:3px;font-size:11px}}.connection b{{color:#9ce0b9}}.connection span{{color:#74838e;font:10px 'DM Mono'}}.quota{{margin-top:12px;padding:9px 10px;border-radius:8px;background:#11171d;display:grid;grid-template-columns:auto 1fr;column-gap:9px;align-items:center}}.quota span,.quota small{{font:10px 'DM Mono';color:#71818c}}.quota strong{{font-size:20px;color:#d5dde0;line-height:1}}.quota small{{grid-column:1 / -1;letter-spacing:0;margin-top:3px}}aside{{margin-top:28px;padding:17px;border-radius:12px;background:#151d24;border:1px solid #293842}}aside b{{font-size:12px}}aside p{{color:#8997a1;font-size:12px;line-height:1.6;margin:5px 0 0}}ul{{display:flex;gap:10px;flex-wrap:wrap;padding:0;margin:10px 0 0}}li{{list-style:none;border:1px solid #31414c;border-radius:7px;padding:7px 9px;font:10px 'DM Mono';color:#b4cbd8}}@media(max-width:780px){{main{{padding:32px 16px}}.cards{{grid-template-columns:repeat(2,1fr)}}header{{display:block}}.readonly{{display:inline-block;margin-top:16px}}}}</style></head><body><main><header><div><div class="eyebrow">SUBROUTE · SOURCE DESK</div><h1>API sources</h1><p>Read-only provider configuration and quota surface.</p></div><div class="readonly">NO ROUTING WRITES</div></header><div class="bar"><b>Separate ownership.</b> A green status means this source has its required gateway configuration. Usage is deliberately separate and only appears when a provider exposes trustworthy quota data.</div><section class="section"><div><div class="eyebrow">CONFIGURED SOURCES</div><h2>Configuration and usage</h2></div><span>PROVIDER-REPORTED ONLY</span></section><section class="cards">{cards}</section><aside><b>Six supported agent connections</b><p>All use their own endpoint recipe while the gateway keeps routing separate.</p><ul>{apps}</ul></aside></main></body></html>'''


async def source_desk(_: Request) -> HTMLResponse:
    script = """<script>
const panel=document.createElement('section');panel.className='routing-desk';panel.innerHTML='<style>.routing-desk{margin:24px 0 32px;padding:22px;border:1px solid #35505e;border-radius:16px;background:linear-gradient(135deg,#17232b,#131a20);box-shadow:0 16px 45px #0002}.route-head{display:flex;justify-content:space-between;align-items:start}.route-title{font-size:18px;font-weight:800;letter-spacing:-.04em}.route-note{font:10px DM Mono,monospace;color:#85bcd9;margin-top:4px}.route-grid{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:12px;margin-top:18px}.route-field{display:grid;gap:7px;font:10px DM Mono,monospace;letter-spacing:.09em;color:#88aabd;text-transform:uppercase}.route-field select{appearance:none;width:100%;padding:11px 30px 11px 11px;background:#0e151a;color:#eef3f4;border:1px solid #3a515f;border-radius:9px;font:600 12px Manrope,sans-serif;background-image:linear-gradient(45deg,transparent 50%,#86b3c9 50%),linear-gradient(135deg,#86b3c9 50%,transparent 50%);background-position:calc(100% - 15px) 50%,calc(100% - 10px) 50%;background-size:5px 5px;background-repeat:no-repeat}.route-field select:focus{outline:0;border-color:#ef8051;box-shadow:0 0 0 3px #ef805122}.route-status{display:flex;justify-content:space-between;gap:10px;margin-top:16px;padding-top:14px;border-top:1px solid #2d414d;font:10px DM Mono,monospace;color:#92a5b0}.route-key{color:#8edbb2}.route-warning{color:#f4b76b}@media(max-width:780px){.route-grid{grid-template-columns:1fr}}</style><div class="route-head"><div><div class="route-title">Routing desk</div><div class="route-note">Choose exactly where new requests go</div></div><div class="route-note">LOCAL POLICY</div></div><div class="route-grid"><label class="route-field">Target model<select id="model"></select></label><label class="route-field">Request policy<select id="mode"><option value="alias">Current aliases only</option><option value="force">Force every new request</option><option value="off">LiteLLM pass through</option></select></label><label class="route-field">Advisor model<select id="advisor"></select></label></div><div class="route-status"><span id="route-status"></span><span class="route-key">LOCAL CLIENT KEY BYPASS</span></div>';
document.querySelector('.section').before(panel);document.querySelector('.readonly').textContent='LOCAL-ONLY CONTROL';const model=panel.querySelector('#model'),mode=panel.querySelector('#mode'),advisor=panel.querySelector('#advisor'),status=panel.querySelector('#route-status');
async function load(){const d=await (await fetch('/api/routing-options')).json();model.innerHTML=d.models.map(x=>`<option value="${x.model_id}">${x.display_name}</option>`).join('');advisor.innerHTML=d.advisor_models.map(x=>`<option value="${x.model_id}">${x.display_name}</option>`).join('');model.value=d.state.active_model;mode.value=d.state.mode;advisor.value=d.state.advisor_model;status.textContent=`${d.client_api_key_required?'Client API key required':'No client API key required'} · policy v${d.state.policy_version}`}
async function save(){const r=await fetch('/api/active-model',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({model:model.value,mode:mode.value})});const d=await r.json();status.textContent=r.ok?`Saved ${d.mode} · policy v${d.policy_version}`:d.detail}async function saveAdvisor(){const r=await fetch('/api/advisor-model',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({advisor_model:advisor.value})});const d=await r.json();status.textContent=r.ok?`Advisor saved · policy v${d.policy_version}`:d.detail}model.onchange=save;mode.onchange=save;advisor.onchange=saveAdvisor;load();</script>"""
    return HTMLResponse(_page().replace("</body>", script + "</body>"))


async def source_status(_: Request) -> dict[str, object]:
    """Safe, read-only status for the standalone desktop companion."""
    return {
        "sources": [
            {"name": source["name"], "configured": source["configured"]}
            for source in _sources()
        ]
    }


def register_ui_routes(app: object) -> None:
    existing = {(getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", None) or []))) for route in app.routes}
    key = ("/control", ("GET",))
    if key not in existing:
        app.add_api_route("/control", source_desk, methods=["GET"], include_in_schema=False, response_class=HTMLResponse)
    status_key = ("/api/source-status", ("GET",))
    if status_key not in existing:
        app.add_api_route("/api/source-status", source_status, methods=["GET"], include_in_schema=False)
