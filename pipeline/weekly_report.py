#!/usr/bin/env python3
"""Generate a weekly supplier insight report (HTML) from the live export."""
import argparse, json, re, datetime, html, sys, os
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import contractor_key as norm, display_for, is_contractor

ap = argparse.ArgumentParser()
ap.add_argument('--sample', dest='sample', action='store_true', default=True,
                help='Label as a sample report (default)')
ap.add_argument('--no-sample', dest='sample', action='store_false',
                help='Generate a clean customer report without sample labeling')
ap.add_argument('--out', required=True)
ap.add_argument('--src', default='/tmp/pp.json')
ap.add_argument('--first-seen', default='/tmp/first_seen.json')
args = ap.parse_args()

SRC = args.src
OUT = args.out
SAMPLE = args.sample

def norm_phone(p):
    d = re.sub(r'\D', '', p or '')
    if len(d) == 11 and d.startswith('1'):
        d = d[1:]
    if len(d) == 10:
        return f'({d[:3]}) {d[3:6]}-{d[6:]}'
    return (p or '').strip() or '—'

def _tel_href(disp):
    d = re.sub(r'\D', '', disp or '')
    if len(d) == 11 and d.startswith('1'):
        d = d[1:]
    if len(d) == 10 and (disp or '').strip() not in ('', '—'):
        return f'tel:+1{d}'
    return None

def tel_cell_raw(disp):
    """Phone display string wrapped as a tap-to-call link (HTML + PDF)."""
    href = _tel_href(disp)
    if href:
        return f'<a href="{href}" style="color:inherit;text-decoration:none;">{esc(disp)}</a>'
    return esc(disp)

def tel_cell(p):
    return tel_cell_raw(norm_phone(p))

def _key_rows(rows):
    g = defaultdict(list)
    for r in rows:
        c = (r.get('contractor') or '').strip()
        if is_contractor(c):
            g[norm(c)].append(c)
    return g

def display_name(rows):
    g = _key_rows(rows)
    if not g: return ''
    k = Counter({k: len(v) for k, v in g.items()}).most_common(1)[0][0]
    return display_for(k, g[k])[:40]

def disp_one(c, key_rows):
    c = (c or '').strip()
    if not c:
        return 'No contractor listed'
    if not is_contractor(c):
        return ''
    k = norm(c)
    vs = key_rows.get(k, [c])
    return display_for(k, vs)[:40]

def phone(rows):
    c = Counter(norm_phone(r.get('contractor_phone')) for r in rows if r.get('contractor_phone'))
    top = c.most_common(1)
    return top[0][0] if top else '—'

def money(v):
    if v >= 1_000_000: return f'${v/1_000_000:.1f}M'
    if v >= 1_000: return f'${v/1_000:.0f}k'
    return f'${v:,.0f}'

def is_elec(r):
    return 'elec' in (r.get('permit_type') or '').lower()

d = json.load(open(SRC))['permits']
today = datetime.date.today()
wk_start = (today - datetime.timedelta(days=7)).isoformat()
prev_start = (today - datetime.timedelta(days=28)).isoformat()

elec = [r for r in d if is_elec(r) and (r.get('applied_date') or '')[:10] >= prev_start]
this_wk = [r for r in elec if (r.get('applied_date') or '')[:10] >= wk_start]
prior = [r for r in elec if (r.get('applied_date') or '')[:10] < wk_start]

# Full-history first-seen lookup (contractor_key -> earliest applied_date),
# built daily by pipeline/contractor_first_seen.py and deployed to
# https://permitpicker.com/data/first_seen.json. Falls back to the 90-day
# export window when the lookup isn't available.
first_seen = {}
try:
    _hist = json.load(open(args.first_seen))
    if isinstance(_hist, dict) and _hist:
        first_seen = _hist
        _hist_from = min(_hist.values())
    else:
        _hist = None
except (FileNotFoundError, json.JSONDecodeError, ValueError):
    _hist = None
if not first_seen:
    _hist_from = None
    for r in sorted(d, key=lambda r: r.get('applied_date') or ''):
        c = norm(r.get('contractor'))
        if c and is_contractor(r.get('contractor')) and c not in first_seen:
            first_seen[c] = (r.get('applied_date') or '')[:10]
new_cs = [c for c, f in first_seen.items()
          if f >= wk_start and any(is_elec(r) and norm(r.get('contractor')) == c for r in this_wk)]

cw = Counter(norm(r.get('contractor')) for r in this_wk if is_contractor(r.get('contractor')))
cp = Counter(norm(r.get('contractor')) for r in prior if is_contractor(r.get('contractor')))
movers = []
for c, n in cw.most_common():
    if c == 'tenant': continue
    base = cp.get(c, 0) / 3.0
    if n >= 3 and n > base * 1.5:
        movers.append((c, n, base))
movers.sort(key=lambda x: x[1] - x[2], reverse=True)

all_elec = [r for r in d if is_elec(r)]
lb = Counter(norm(r.get('contractor')) for r in all_elec
             if is_contractor(r.get('contractor')))
by_c = defaultdict(list)
for r in all_elec:
    c0 = (r.get('contractor') or '').strip()
    if is_contractor(c0):
        by_c[norm(c0)].append(r)

vals = []
for r in this_wk:
    try:
        v = float(r.get('valuation') or 0)
        if v > 0: vals.append(v)
    except (TypeError, ValueError):
        pass
val_total = sum(vals)
val_med = sorted(vals)[len(vals)//2] if vals else 0

def esc(s): return html.escape(str(s or ''))
def clean_text(t, maxlen=None):
    t = re.sub(r'\s+', ' ', t or '').strip()
    if maxlen and len(t) > maxlen:
        cut = t[:maxlen].rsplit(' ', 1)[0]
        t = (cut if cut else t[:maxlen]) + '\u2026'
    return t
week_label = f"{wk_start} – {today.isoformat()}"

def val_cell(r):
    try:
        v = float(r.get('valuation') or 0)
        return money(v) if v > 0 else '—'
    except (TypeError, ValueError):
        return '—'

wk_names = _key_rows(this_wk)
lead_rows = []
for r in sorted(this_wk, key=lambda r: r.get('applied_date') or '', reverse=True):
    lead_rows.append(
        f"<tr><td>{esc((r.get('applied_date') or '')[:10])}</td><td>{esc(clean_text(r.get('address')))}</td>"
        f"<td>{esc(clean_text((r.get('city') or '').title()))}</td><td>{esc(clean_text(r.get('description'), 60))}</td>"
        f"<td data-v='{esc(r.get('valuation') or 0)}'>{val_cell(r)}</td><td>{esc(disp_one(r.get('contractor'), wk_names))}</td>"
        f"<td>{tel_cell(r.get('contractor_phone'))}</td></tr>")

new_html = ''.join(
    f"<tr><td>{esc(display_name(by_c[c]))}</td><td>{tel_cell_raw(phone(by_c[c]))}</td>"
    f"<td>{esc(first_seen[c])}</td><td>{len(by_c[c])}</td></tr>" for c in new_cs[:10])

mover_html = ''.join(
    f"<tr><td>{esc(display_name(by_c[c]))}</td><td>{tel_cell_raw(phone(by_c[c]))}</td>"
    f"<td>{n}</td><td>{base:.1f}/wk prior</td></tr>" for c, n, base in movers[:8])

lb_html = ''.join(
    f"<tr><td>{i+1}</td><td>{esc(display_name(by_c[c]))}</td><td>{tel_cell_raw(phone(by_c[c]))}</td>"
    f"<td>{n}</td></tr>" for i, (c, n) in enumerate(lb.most_common(10)))

phones = sum(1 for r in this_wk if r.get('contractor_phone'))

page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PermitPulse — Weekly Electrical Intel{" (Sample)" if SAMPLE else ""}</title>
<style>
:root{{--ink:#1c2b33;--muted:#5b6b73;--accent:#0d7a6f;--accent-dark:#0a5f57;--line:#e3e9eb;--bg:#f7faf9}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;margin:0;color:var(--ink);background:var(--bg);line-height:1.5}}
.masthead{{background:#102e36;color:#fff;padding:26px 24px}}
.masthead-inner{{max-width:1000px;margin:0 auto}}
.brand{{font-size:24px;font-weight:800;letter-spacing:.5px}}
.brand .pulse{{color:#5eead4}}
.brand-sub{{font-size:13px;color:#b9cdc9;margin-top:2px}}
main{{max-width:1000px;margin:0 auto;padding:8px 24px 40px}}
.report-head{{padding:26px 0 6px}}
h1{{font-size:30px;margin:0 0 4px;letter-spacing:-.5px}}
.sub{{color:var(--muted);font-size:14px;margin-bottom:6px}}
h2{{font-size:18px;margin:0 0 4px;color:var(--accent-dark);padding-bottom:6px;border-bottom:2px solid var(--accent)}}
section{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:22px 24px;margin-top:22px;box-shadow:0 1px 2px rgba(16,46,54,.05)}}
section > p{{color:var(--muted);font-size:14px;margin:4px 0 12px}}
.stats{{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0 4px}}
.stat{{background:#f0faf8;border:1px solid #cde8e3;border-left:4px solid var(--accent);border-radius:8px;padding:12px 18px;min-width:140px;flex:1}}
.stat b{{font-size:26px;color:var(--accent-dark);display:block;line-height:1.1}}
.stat span{{font-size:12.5px;color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:10px}}
thead th{{position:sticky;top:0;background:#f2f6f6}}
th{{text-align:left;padding:9px 10px;border-bottom:2px solid var(--line);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);cursor:pointer;white-space:nowrap}}
th:hover{{background:#e9efef}}
td{{padding:8px 10px;border-bottom:1px solid #eef2f2;vertical-align:top}}
tbody tr:hover td{{background:#f7fbfa}}
.table-scroll{{overflow-x:auto;margin:10px -24px 0;padding:0 24px}}
#leads{{min-width:720px}}
.filter{{margin:10px 0;padding:10px 14px;font-size:14px;width:100%;max-width:440px;border:1px solid #c8d4d6;border-radius:8px}}
.note{{font-size:12.5px;color:var(--muted);margin:26px 0 0;border-top:1px solid var(--line);padding-top:14px}}
.tag{{display:inline-block;background:#0d7a6f;color:#fff;border-radius:4px;padding:1px 7px;font-size:11px;font-weight:700;letter-spacing:.3px}}
.cta{{margin-top:22px;background:#102e36;color:#fff;border-radius:10px;padding:20px 24px}}
.cta b{{font-size:16px}}
.cta span{{font-size:13px;color:#b9cdc9;display:block;margin-top:4px;max-width:640px}}
@page{{size:letter;margin:0.6in 0.55in}}
@media print{{
  .masthead{{background:none;padding:0 0 10px}}
  .masthead .brand{{color:#102e36}}
  .masthead .brand .pulse{{color:#0d7a6f}}
  .masthead .brand-sub{{color:#5b6b73}}
  .masthead-inner{{max-width:none}}
  main{{max-width:none;padding:0}}
  section{{box-shadow:none;page-break-inside:avoid}}
  thead{{display:table-header-group}}
  thead th{{position:static}}
  tr{{page-break-inside:avoid}}
  .no-print,.cta{{display:none}}
  body{{background:#fff}}
  table{{font-size:12px}}
  .stats{{flex-wrap:nowrap;gap:10px}}
  .stat{{min-width:0;padding:10px 12px}}
  .stat b{{font-size:22px}}
  .stat span{{font-size:11px}}
  #leads{{table-layout:fixed;width:100%}}
  #leads th,#leads td{{white-space:normal;word-break:break-word;padding:7px 10px}}
  #leads td:first-child,#leads th:first-child{{white-space:nowrap}}
  #leads th{{font-size:10px}}
  .table-scroll{{margin:10px 0 0;padding:0;overflow:visible}}
}}
@media (max-width:640px){{
  .stat{{min-width:calc(50% - 7px)}}
  h1{{font-size:24px}}
}}
</style></head><body>
<header class="masthead"><div class="masthead-inner">
<div class="brand">Permit<span class="pulse">Pulse</span></div>
<div class="brand-sub">Permit intelligence for electrical distributors · by PermitPicker</div>
</div></header>
<main>
<div class="report-head">
<h1>Weekly Electrical Intel</h1>
<div class="sub">Raleigh area · {week_label}{' · Sample report' if SAMPLE else ''}</div>
</div>

<section>
<div class="stats">
<div class="stat"><b>{len(this_wk)}</b><span>new electrical permits</span></div>
<div class="stat"><b>{phones}</b><span>with contractor phone</span></div>
<div class="stat"><b>{len(cw)}</b><span>active contractors</span></div>
<div class="stat"><b>{len(new_cs)}</b><span>new to our records</span></div>
<div class="stat"><b>{money(val_total)}</b><span>total job valuation</span></div>
<div class="stat"><b>{money(val_med)}</b><span>median job valuation</span></div>
</div>
</section>

<section>
<h2>New to our records this week</h2>
<p>Contractors appearing in our records for the first time{f" — checked against full history back to {_hist_from}" if _hist_from else ""}.</p>
<table><tr><th>Contractor</th><th>Phone</th><th>First seen</th><th>Permits</th></tr>{new_html or '<tr><td colspan=4>None this week</td></tr>'}</table>
</section>

<section>
<h2>Biggest movers</h2>
<p>Contractors running well above their recent pace (this week vs. prior 3-week average).</p>
<table><tr><th>Contractor</th><th>Phone</th><th>This week</th><th>Prior avg</th></tr>{mover_html or '<tr><td colspan=4>None this week</td></tr>'}</table>
</section>

<section>
<h2>90-day leaderboard</h2>
<p>Most active electrical contractors in our records over the last 90 days.</p>
<table><tr><th>#</th><th>Contractor</th><th>Phone</th><th>Permits</th></tr>{lb_html}</table>
</section>

<section>
<h2>All permits this week</h2>
<input class="filter no-print" id="q" type="search" placeholder="Filter by address, contractor, description…  (click a column header to sort)">
<div class="table-scroll"><table id="leads"><colgroup><col style="width:14%"><col style="width:17%"><col style="width:8%"><col style="width:27%"><col style="width:8%"><col style="width:14%"><col style="width:12%"></colgroup><thead><tr><th>Filed</th><th>Address</th><th>City</th><th>Description</th><th>Value</th><th>Contractor</th><th>Phone</th></tr></thead>
<tbody>{''.join(lead_rows)}</tbody></table></div>
</section>

{('<div class="cta no-print"><b>Like what you see?</b><span>This is a sample. PermitPulse delivers this briefing every week — plus a daily alert when new electrical permits are filed in your territory. Built from public permit records, refreshed daily.</span></div>' if SAMPLE else '<div class="cta no-print"><b>PermitPulse weekly briefing</b><span>Delivered every week, plus a daily alert when new electrical permits are filed in your territory. Built from public permit records, refreshed daily.</span></div>')}

<div class="note">{"Sample generated" if SAMPLE else "Generated"} {today.isoformat()} from public permit records (Wake County, City of Raleigh &amp; Town of Cary).
"First seen" reflects PermitPulse's tracking window. Trade permits do not report valuations — totals and medians reflect only permits with a reported value. Coverage: county-issued Wake permits + City of Raleigh + Town of Cary; not all municipalities. Data is compiled from public records and provided as-is for informational purposes; PermitPulse does not warrant its accuracy, completeness, or timeliness and is not affiliated with any issuing authority.</div>
</main>

<script>
const q = document.getElementById('q'), tb = document.querySelector('#leads tbody');
q.addEventListener('input', () => {{
  const s = q.value.toLowerCase();
  for (const tr of tb.rows) tr.style.display = tr.textContent.toLowerCase().includes(s) ? '' : 'none';
}});
document.querySelectorAll('#leads th').forEach((th, i) => {{
  th.addEventListener('click', () => {{
    const rows = [...tb.rows], asc = th.dataset.asc !== '1';
    th.dataset.asc = asc ? '1' : '0';
    rows.sort((a, b) => {{
      let x = a.cells[i].textContent, y = b.cells[i].textContent;
      if (i === 4) {{ x = parseFloat(a.cells[i].dataset.v || 0); y = parseFloat(b.cells[i].dataset.v || 0); }}
      return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1);
    }});
    rows.forEach(r => tb.appendChild(r));
  }});
}});
</script>
</body></html>
"""

open(OUT, 'w').write(page)
print(f'report -> {OUT}  ({len(page)//1024}KB)')
pdf_out = OUT.rsplit('.', 1)[0] + '.pdf'
try:
    from weasyprint import HTML as _WHTML
    _WHTML(string=page).write_pdf(pdf_out)
    import os as _os
    print(f'report -> {pdf_out}  ({_os.path.getsize(pdf_out)//1024}KB)')
except Exception as e:
    print(f'PDF skipped ({e})')
