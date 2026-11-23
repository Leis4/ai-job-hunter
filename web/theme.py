"""AFO visual system: light, user-first, responsive. No external assets."""
from __future__ import annotations

import html

CSS = """
:root{--bg:#f5f7fb;--white:#fff;--ink:#172038;--muted:#75819a;--soft:#9aa6bd;--border:#e6ebf4;
--blue:#3157d5;--blue-2:#2446b8;--bluelight:#eef3ff;--green:#0f7a5c;--greenlight:#e9f7f1;
--amber:#9a6414;--amberlight:#fff4dd;--red:#a33245;--redlight:#fdeef1;--violet:#6b4fd8;
--shadow:0 10px 30px rgba(27,42,79,.06);--radius:18px;--hit:44px}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
a{color:inherit;text-decoration:none}a:hover{color:var(--blue)}
button,input,select,textarea{font:inherit;color:inherit}
button,.btn{min-height:var(--hit);display:inline-flex;align-items:center;justify-content:center;gap:8px;
border:1px solid transparent;background:var(--blue);color:#fff;border-radius:12px;padding:10px 16px;
font-weight:650;cursor:pointer;box-shadow:0 4px 12px rgba(49,87,213,.16);transition:background .15s,box-shadow .15s}
button:hover,.btn:hover{background:var(--blue-2);color:#fff}
button.secondary,.btn.secondary{background:#fff;color:var(--blue);border-color:var(--border);box-shadow:none}
button.secondary:hover,.btn.secondary:hover{background:var(--bluelight)}
button.ghost,.btn.ghost{background:transparent;color:var(--muted);border-color:transparent;box-shadow:none}
button.ghost:hover{background:#eef2f9;color:var(--ink)}
button.danger{background:var(--red);box-shadow:none}button.danger:hover{background:#8b2839}
button.tiny{min-height:var(--hit);padding:8px 12px;border-radius:10px;background:#fff;color:var(--ink);
border:1px solid var(--border);box-shadow:none;font-size:13px;font-weight:600}
button.tiny:hover{background:var(--bluelight);color:var(--blue)}
button:disabled{opacity:.5;cursor:not-allowed;box-shadow:none}
:focus-visible{outline:3px solid rgba(49,87,213,.35);outline-offset:2px}
.shell{display:grid;grid-template-columns:240px minmax(0,1fr);min-height:100vh;min-width:0}
.sidebar{background:var(--white);border-right:1px solid var(--border);padding:28px 18px;
display:flex;flex-direction:column;gap:26px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:12px;font-weight:780;font-size:17px;letter-spacing:-.4px;padding:0 6px}
.logo{width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg,#3f6ae8,#6b4fd8);color:#fff;
display:grid;place-items:center;font-size:20px;box-shadow:0 6px 16px rgba(63,106,232,.25)}
.nav{display:grid;gap:5px}
.nav a{min-height:var(--hit);display:flex;align-items:center;gap:10px;padding:10px 13px;border-radius:12px;
color:#5c6980;font-weight:560;font-size:14.5px}
.nav a:hover{background:#f3f6fc;color:var(--ink)}
.nav a.active{background:var(--bluelight);color:var(--blue);font-weight:700}
.nav .dot{width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.35}
.nav a.active .dot{opacity:1}
.foot{margin-top:auto;padding:14px;border:1px solid var(--border);border-radius:14px;color:var(--muted);
font-size:12px;line-height:1.55;background:#fbfcff}
.main{width:100%;max-width:1220px;margin:0 auto;padding:34px 40px 80px;min-width:0}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:24px;flex-wrap:wrap}
.eyebrow{color:var(--muted);font-size:12.5px;font-weight:650;letter-spacing:.04em;text-transform:uppercase}
h1{font-size:30px;line-height:1.15;letter-spacing:-.9px;margin:5px 0 0}
h2{font-size:19px;letter-spacing:-.4px;margin:0}
h3{font-size:16px;margin:0}
.sub{color:var(--muted);font-size:13.5px;margin:4px 0 0}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:0 0 20px}
.metric{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);
padding:20px 22px;box-shadow:var(--shadow)}
.metric .label{color:var(--muted);font-size:12.5px;font-weight:600}
.metric .value{font-size:34px;font-weight:760;letter-spacing:-1.2px;margin-top:5px;line-height:1.1}
.metric .helper{font-size:12px;color:var(--green);margin-top:5px}
.metric.warn .helper{color:var(--amber)}
.metric.muted .helper{color:var(--muted)}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:18px;align-items:start}
.panel{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);
padding:22px;box-shadow:var(--shadow)}
.panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}
.job{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:16px 0;
border-bottom:1px solid var(--border)}
.job:last-child{border-bottom:0;padding-bottom:0}.job:first-child{padding-top:0}
.job .company{font-size:12.5px;color:var(--muted);margin-bottom:4px}
.job h3{font-size:16.5px;margin:0 0 5px;letter-spacing:-.2px}
.job .description{font-size:13px;color:#66738b;max-width:560px}
.job .reasons{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.jobright{display:flex;align-items:center;gap:12px;flex-shrink:0}
.score{width:56px;height:56px;border-radius:50%;display:grid;place-items:center;position:relative;
background:conic-gradient(var(--ring,#0f7a5c) calc(var(--pct,0)*1%),#e8eef7 0);font-weight:800;font-size:15px;
color:var(--ring,#0f7a5c)}
.score::before{content:'';position:absolute;inset:5px;border-radius:50%;background:#fff}
.score span{position:relative;z-index:1}
.score small{font-size:9px;font-weight:600;color:var(--muted);display:block;text-align:center;line-height:1}
.chip{display:inline-flex;align-items:center;gap:5px;border-radius:20px;padding:3px 9px;background:#f1f4fa;
color:#5c6980;font-size:11.5px;font-weight:600;border:1px solid #e7ecf5;white-space:nowrap}
.chip.good{background:var(--greenlight);color:var(--green);border-color:#cdeee1}
.chip.warn{background:var(--amberlight);color:var(--amber);border-color:#f3e0b8}
.chip.bad{background:var(--redlight);color:var(--red);border-color:#f4d2d9}
.chip.info{background:var(--bluelight);color:var(--blue);border-color:#dbe5ff}
.chip.violet{background:#f2efff;color:var(--violet);border-color:#e3dcff}
.notice{border:1px solid #dce5f5;background:#f8faff;border-radius:14px;padding:13px 15px;color:#4e5d77;
font-size:13.5px;margin:0 0 18px}
.notice.good{background:var(--greenlight);border-color:#cdeee1;color:#146b52}
.notice.warn{background:var(--amberlight);border-color:#f3e0b8;color:#7d5410}
.notice.bad{background:var(--redlight);border-color:#f4d2d9;color:#8b2839}
.notice a{font-weight:650;color:var(--blue);text-decoration:underline;text-underline-offset:2px}
.empty{border:1px dashed #d7e0ef;background:#fbfcff;border-radius:16px;padding:26px;text-align:center;color:var(--muted);font-size:13.5px}
.empty b{display:block;color:var(--ink);margin-bottom:5px;font-size:15px}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0 18px}
.toolbar .grow{flex:1;min-width:180px}
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-size:12.5px;font-weight:650;color:#56627a}
input[type=text],input[type=search],input[type=number],input[type=file],select,textarea{
background:#fff;border:1px solid #dbe3f0;border-radius:11px;padding:10px 12px;min-height:44px;width:100%;
outline:none}
textarea{min-height:130px;resize:vertical;line-height:1.5}
input:focus,select:focus,textarea:focus{border-color:#8aa6f5;box-shadow:0 0 0 3px rgba(49,87,213,.12)}
.search{position:relative}.search input{padding-left:38px}.search::before{content:'⌕';position:absolute;left:14px;
top:10px;color:var(--muted);font-size:18px;pointer-events:none}
.check{display:flex;align-items:flex-start;gap:9px;font-size:13.5px;padding:6px 0}
.check input{width:18px;height:18px;min-height:0;margin-top:2px;accent-color:var(--blue)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;border-bottom:1px solid var(--border);margin:8px 0 20px}
.tabs a,.tabs button{min-height:42px;display:inline-flex;align-items:center;padding:8px 14px;border-radius:11px 11px 0 0;
background:transparent;border:0;box-shadow:none;color:var(--muted);font-weight:650;font-size:13.5px;cursor:pointer}
.tabs a:hover,.tabs button:hover{background:#f4f7fd;color:var(--ink)}
.tabs a.active,.tabs button.active{background:var(--white);color:var(--blue);box-shadow:inset 0 -3px 0 var(--blue)}
.card{background:var(--white);border:1px solid var(--border);border-radius:16px;padding:18px;box-shadow:var(--shadow)}
.grid{display:grid;gap:14px;align-items:start}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.g4{grid-template-columns:repeat(4,minmax(0,1fr))}
.list{display:flex;flex-direction:column;gap:10px}
.mini{border:1px solid var(--border);border-radius:14px;padding:12px 14px;background:#fbfcff}
.mini b{display:block;font-size:13.5px;margin-bottom:2px}.mini small{color:var(--muted)}
.kv{display:grid;grid-template-columns:140px minmax(0,1fr);gap:8px 14px;font-size:13.5px}
.kv .k{color:var(--muted)}
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:14px;background:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #eef2f8;vertical-align:top}
th{background:#f8faff;color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}
.letter{background:#fbfcff;border:1px dashed #cbd8ee;border-radius:13px;padding:15px;white-space:pre-wrap;
font-size:13.5px;line-height:1.6}
.progress{height:8px;background:#edf1f8;border-radius:99px;overflow:hidden}
.progress>i{display:block;height:100%;background:linear-gradient(90deg,#4f7bf0,#7a5be8);border-radius:99px}
.divider{height:1px;background:var(--border);margin:16px 0}
.muted{color:var(--muted)}.small{font-size:12.5px}.right{text-align:right}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.spacer{flex:1}
.steps{list-style:none;padding:0;margin:12px 0 0;display:flex;flex-direction:column;gap:11px}
.steps li{display:flex;gap:10px;font-size:13.5px;color:#53617a}
.steps b{width:25px;height:25px;border-radius:8px;background:var(--bluelight);color:var(--blue);
display:grid;place-items:center;flex-shrink:0;font-size:12px}
details{border:1px solid var(--border);border-radius:13px;background:#fbfcff;padding:12px 14px;margin:10px 0}
summary{cursor:pointer;font-weight:650;color:#4a5872;font-size:13.5px}
.msg{border:1px solid var(--border);border-left:4px solid #9fb4da;border-radius:12px;padding:12px 14px;background:#fff;margin:10px 0}
.msg.rejection{border-left-color:var(--red)}.msg.invitation{border-left-color:var(--green)}
.msg.test_task{border-left-color:#c98a1e}.msg.offer{border-left-color:var(--blue)}
.bar{height:8px;background:#edf1f8;border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#4f7bf0,#7a5be8);border-radius:99px}
.section-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:26px 0 12px}
.section-title h2{margin:0}
.mobile-only{display:none}
html[data-theme="dark"]{--bg:#0b1220;--white:#121a2b;--ink:#e7eefc;--muted:#93a3bf;--soft:#7e8daa;
--border:#24314a;--bluelight:#17233d;--shadow:0 10px 30px rgba(0,0,0,.22)}
html[data-theme="dark"] body{background:var(--bg);color:var(--ink)}
html[data-theme="dark"] .sidebar,html[data-theme="dark"] .panel,html[data-theme="dark"] .card,
html[data-theme="dark"] .metric{background:var(--white)}
html[data-theme="dark"] .mini,html[data-theme="dark"] details,html[data-theme="dark"] .empty,
html[data-theme="dark"] .letter{background:#0f1728;border-color:var(--border)}
html[data-theme="dark"] input,html[data-theme="dark"] select,html[data-theme="dark"] textarea{background:#0d1626;color:var(--ink);border-color:#2a3a58}
html[data-theme="dark"] table{background:var(--white)}
html[data-theme="dark"] th{background:#16213a;color:var(--muted)}
html[data-theme="dark"] tr:hover td{background:#16233b}
html[data-theme="dark"] .job .description,html[data-theme="dark"] .steps li{color:#a7b4cb}
html[data-theme="dark"] .notice{background:#101a2c;border-color:#2a3a58;color:#bac9e2}
html[data-theme="dark"] .score::before{background:#121a2b}
html[data-theme="dark"] button.tiny,html[data-theme="dark"] .theme-toggle{background:#16213a;color:#dbe7ff;border-color:#2a3a58}
html[data-theme="dark"] .btn.ghost{color:#a9b8d4}
html[data-theme="dark"] .metric .helper{color:#7fd8b4}
html[data-theme="dark"] .tabs a.active{background:var(--white)}
html[data-theme="dark"] .foot{background:#0f1728}
.theme-toggle{width:100%;margin-top:8px;min-height:40px;padding:7px 10px;font-size:12px}
@media(max-width:1050px){.shell{grid-template-columns:1fr}.sidebar{position:static;height:auto;
flex-direction:row;align-items:center;gap:14px;padding:12px 16px;border-right:0;border-bottom:1px solid var(--border);
overflow-x:auto}.brand{font-size:15px;white-space:nowrap}.nav{display:flex;gap:4px}.nav a{white-space:nowrap;padding:9px 11px}
.nav .dot{display:none}.foot{display:none}.main{padding:24px 20px 60px}.workspace{grid-template-columns:1fr}
.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:720px){.sidebar{flex-wrap:wrap}.brand{width:100%}.nav{display:flex;flex-wrap:wrap;gap:4px;overflow:visible}.nav a{white-space:nowrap;padding:8px 9px;font-size:13px}
.main{padding:20px 14px 56px}h1{font-size:25px}.top{gap:14px}.actions{width:100%}
.actions .btn,.actions button{flex:1}.metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
body{overflow-x:hidden}.toolbar{display:grid;grid-template-columns:1fr}.toolbar>*{width:100%;max-width:100%!important;min-width:0}
.chip{white-space:normal;max-width:100%;word-break:break-word}
.metric{padding:13px 12px;border-radius:13px}.metric .value{font-size:25px}.metric .label{font-size:10.5px}
.metric .helper{font-size:9.5px}.g2,.g3,.g4{grid-template-columns:1fr}.job{align-items:flex-start}
.jobright{flex-direction:column;gap:8px}.job .description{max-width:230px}.panel{padding:16px;border-radius:16px}
.kv{grid-template-columns:1fr}.kv .k{margin-bottom:-6px}.table-wrap{font-size:12px}
th,td{padding:9px 8px}.mobile-only{display:block}.desktop-only{display:none}
}
"""

NAV = [
    ("/", "Сегодня"),
    ("/jobs", "Вакансии"),
    ("/queue", "Мои отклики"),
    ("/resume", "Резюме"),
    ("/settings", "Настройки"),
]


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def layout(title: str, content: str, *, active: str = "/", flash: tuple[str, str] | None = None,
           refresh: int = 0) -> bytes:
    nav = "".join(
        f'<a href="{href}" class="{"active" if href == active else ""}">'
        f'<span class="dot"></span>{esc(label)}</a>'
        for href, label in NAV
    )
    flash_html = ""
    if flash and flash[0]:
        kind = "good" if flash[1] == "ok" else ("bad" if flash[1] == "err" else "warn")
        flash_html = f'<div class="notice {kind}" role="status">{esc(flash[0])}</div>'
    markup = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{'<meta http-equiv="refresh" content="' + str(refresh) + '">' if refresh else ''}
<title>{esc(title)} · AI Job Hunter</title><style>{CSS}</style></head><body>
<div class="shell"><aside class="sidebar">
<div class="brand"><div class="logo">✦</div><span>AI Job Hunter</span></div>
<nav class="nav" aria-label="Основная навигация">{nav}</nav>
<button class="btn ghost theme-toggle" type="button" onclick="toggleTheme()" aria-label="Переключить тему">◐ Светлая / тёмная тема</button>
<div class="foot">Личный помощник в поиске работы.<br>Данные и сессия остаются на этом ПК.</div>
</aside><main class="main">{flash_html}{content}</main></div>
<script>
(function(){{var t=localStorage.getItem('ajh-theme');if(t)document.documentElement.dataset.theme=t;}})();
function toggleTheme(){{var d=document.documentElement;var next=d.dataset.theme==='dark'?'light':'dark';d.dataset.theme=next;localStorage.setItem('ajh-theme',next);}}
</script></body></html>"""
    return markup.encode("utf-8")
