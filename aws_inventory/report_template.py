"""Localized markup for the standalone HTML dashboard."""
import base64
import hashlib
from html import escape

from .common import compact_json
from .localization import LOCALES, labels, display_datetime, translated_messages
from .service_exports import embedded_workbooks


NOTE_TRANSLATIONS = {
    "Instance runtime": "Utilizzo delle istanze", "Provisioned storage": "Storage allocato",
    "Additional IOPS": "IOPS aggiuntive", "Additional throughput": "Throughput aggiuntivo",
    "Base provisioned hours": "Ore di disponibilità della risorsa",
    "Fargate memory GiB": "Memoria Fargate in GiB",
    "Provisioned capacity persists even when its instance is stopped. Partial months are prorated by calendar hours.": "La capacità allocata resta presente anche con istanza arrestata. I mesi parziali sono proporzionati alle ore di calendario.",
    "gp3 includes 3,000 IOPS and 125 MiB/s; only provisioned excess is priced.": "gp3 include 3.000 IOPS e 125 MiB/s; viene prezzata solo la parte allocata in eccesso.",
    "RDS compute only; DB storage, backups and I/O are not priced by this collector.": "Solo compute RDS: storage del database, backup e I/O non vengono stimati da questo collector.",
    "Current data-node compute only; dedicated masters, warm nodes, storage, backups and usage charges excluded.": "Solo compute dei nodi dati attuali; master dedicati, nodi warm, storage, backup e consumi esclusi.",
    "Base provisioned hours only, 24 h/day. Traffic, capacity units, data processing and other network charges excluded; endpoint quantity uses one subnet per AZ.": "Solo quota oraria di base, 24 ore/giorno. Traffico, unità di capacità, elaborazione dati e altri costi di rete esclusi; gli endpoint usano una subnet per zona come quantità.",
    "Fargate current task capacity and assumed runtime; ephemeral storage above the included capacity, platform extras and transfer excluded.": "Capacità attuale del task Fargate e durata ipotizzata; storage effimero oltre la quota inclusa, extra di piattaforma e trasferimenti esclusi.",
    "Current stopped/deleted state assumed throughout the scenario; compute usage is zero.": "Lo stato attuale arrestato/eliminato è ipotizzato per tutto lo scenario; utilizzo compute pari a zero.",
    "Node count unavailable; no cluster compute estimate.": "Numero di nodi non disponibile; compute del cluster non stimato.",
    "Missing or ambiguous volume capacity price/size.": "Tariffa o capacità del volume mancante o ambigua.",
    "Spot resource: estimate uses On-Demand comparison, not the Spot rate.": "Risorsa Spot: la stima usa il confronto On-Demand, non la tariffa Spot.",
    "Catalog components use different currencies; component excluded.": "Le componenti di catalogo usano valute diverse; componente esclusa.",
    "Capacity only; any I/O or other volume-type charges are excluded.": "Solo capacità; I/O e altri costi specifici del tipo di volume esclusi.",
}


def render_template(model, css, script):
    language = model["context"].get("language", "en")
    w = labels(language)
    messages = translated_messages(language)
    notes = NOTE_TRANSLATIONS if language == "it" else {}
    t = lambda key: escape(w[key])
    # Omit raw configuration metadata entirely from the standalone HTML payload.
    public_model = {**model, **{key: [{k: v for k, v in row.items() if k != "configuration"}
                                    for row in model[key]] for key in ("resources", "structural")}}
    payload = compact_json({**public_model, "labels": w, "note_translations": notes, "messages": messages, "locale": LOCALES[language], "workbooks": embedded_workbooks(model)}).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    csp = "default-src 'none'; base-uri 'none'; form-action 'none'; " + f"script-src 'sha256-{digest}'; style-src 'unsafe-inline'; img-src data:"
    notices = ([w["simulation"]] if model["simulation_only"] else []) + ([w["read_errors"]] if model["partial"] else [])
    currencies = {b["currency"] for b in model["billing"]}
    currencies.update(entry["currency"] for r in model["resources"] for entry in r["months"].values())
    if currencies - {"", "USD"}:
        notices.append(w["non_usd"])
    last = model["windows"][-1]["month"]
    fallback = []
    for r in model["resources"]:
        amounts = r["totals"][last]["amounts"]
        if "USD" in amounts:
            total = f"USD {amounts['USD']:.2f}"
            rate = r["plan"]["rate_label"] if r["plan"]["currency"] == "USD" else w["unavailable"]
            fallback.append(f"<tr><td>{escape(r['name'])}</td><td>{escape(rate)}</td><td>{escape(total)}</td></tr>")
    issues = "".join(f"<li>{escape(str(i.get('service', '')))} / {escape(str(i.get('region', '')))}: {escape(str(i.get('notes', '')))}</li>" for i in model["issues"])
    error_details = f'<details class="panel"><summary>{t("read_errors")}</summary><ul>{issues}</ul></details>' if issues else ""
    generated = display_datetime(model["generated_at"], model["context"].get("date_format", "iso"), model["context"].get("timezone", "UTC"))
    return f'''<!doctype html>
<html lang="{language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{escape(csp, quote=True)}"><title>{t('title')} · {escape(str(model['account']))}</title>
<style>{css}</style></head><body>
<header class="app-header"><nav id="section-nav" aria-label="{t('sections')}"></nav><button id="theme" class="theme-toggle" type="button" role="switch" aria-checked="false" aria-label="{t('theme')}" title="{t('theme')}"><span aria-hidden="true">☀</span><span aria-hidden="true">☾</span></button></header>
<aside><div class="nav-title">{t('services')}</div><nav id="service-nav" aria-label="{t('services')}"></nav></aside>
<main><div class="top"><h1 id="scope-title">{t('overview')}</h1><p class="muted">{t('account')} {escape(str(model['account']))} · {escape(generated)}</p></div>
<div class="controls"><label>{t('month')}<select id="month"></select></label><label>{t('region')}<select id="region"><option value="">{t('all_regions')}</option></select></label><label>{t('service')}<select id="service"><option value="">{t('all_services')}</option></select></label><label class="search-field">{t('search_label')}<input id="search" type="search" placeholder="{t('search')}"></label></div>
<div class="banner">{escape(' '.join(notices))}</div><p id="period-note" class="metric-note"></p>
<div id="view-content"></div><template id="view-overview"><section id="overview" class="stats"><div class="stat primary"><div class="label">{t('model')}</div><strong id="modeled-total">—</strong><small>{t('model_note')}</small></div><div class="stat"><div class="label">{t('estimated')}</div><strong id="estimated-total">—</strong><small>{t('estimate_note')}</small></div><div class="stat"><div class="label">{t('bill_total')}</div><strong id="billed-total">—</strong><small>{t('billing_note')}</small></div></section>
<p id="coverage-note" class="metric-note"></p><div class="grid"><section class="panel"><h3>{t('trend')}</h3><small><span class="legend model"></span> {t('model')} · <span class="legend bill"></span> {t('billing')}</small><div id="trend" class="chart"></div></section><section class="panel"><h3 id="service-chart-title">{t('chart_services')}</h3><div id="service-chart"></div></section></div>
<section class="panel spaced-panel"><h3>{t('chart_regions')}</h3><div id="region-chart"></div></section>
</template><template id="view-resources"><section id="resource-section"><div class="section-head"><h2>{t('resources')}</h2><small id="result-count"></small></div><div id="resources"></div></section>
</template><noscript><p>{t('no_js')}</p><table>{''.join(fallback)}</table></noscript>
<template id="view-billing"><section id="billing" class="panel spaced-panel"><h2>{t('billing')}</h2><p class="muted">{t('billing_note')}</p><p id="shared-billing" class="metric-note"></p><div id="billing-table"></div></section>
</template><template id="view-exports"><section class="panel export-panel"><h2>{t('exports')}</h2><p class="muted">{t('export_note')}</p><div class="export-actions"><label>{t('service')}<select id="export-service"></select></label><button id="export-resources" type="button">{t('export_resources')}</button></div><p id="export-status" role="status" aria-live="polite"></p><div class="export-sheets"><div><b>{t('billing')}</b><p>{t('sheet_billing')}</p></div><div><b>{t('by_type')}</b><p>{t('sheet_type')}</p></div><div><b>{t('by_resource')}</b><p>{t('sheet_resource')}</p></div><div><b>{t('attachments')}</b><p>{t('sheet_attached')}</p></div></div></section></template>
{error_details}
<footer class="footer">{t('footer')}</footer></main>
<script id="report-data" type="application/json">{payload}</script><script>{script}</script></body></html>'''
