'use strict';
const data=JSON.parse(document.getElementById('report-data').textContent);
const $=id=>document.getElementById(id), words=data.labels, t=key=>words[key]??key;
const language=data.context.language, locale=data.locale;
const tr=(english,italian)=>language==='it'?italian:(data.messages[english]??english);
const tf=(key,values)=>t(key).replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??''));
// Build immutable lookup data once; navigation only renders the selected view.
const resources = data.resources;
const byKey = new Map(resources.map(resource => [resource.key, resource]));
const windowsByMonth = new Map(data.windows.map(window => [window.month, window]));
const amountFormatter = new Intl.NumberFormat(locale, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const quantityFormatter = new Intl.NumberFormat(locale, {maximumFractionDigits: 8});
const monthFormatter = new Intl.DateTimeFormat(locale, {month: 'short', year: 'numeric', timeZone: 'UTC'});
const monthLabels = new Map(data.windows.map(window => [window.month, monthFormatter.format(new Date(window.month + '-01T00:00:00Z'))]));
const serviceKeys = Object.keys(data.workbooks);
const resourcesByService = groupBy(resources, resource => resource.service);
const billingByMonth = groupBy(data.billing, bill => `${bill.month}|${bill.currency}`);
const billingNames = new Map(Object.entries(data.billing_services).map(([service, names]) => [service, new Set(names)]));
const inventory = [...resources, ...(data.structural ?? []).map((resource, index) => ({...resource, key: 'structure-' + index}))]
    .sort((left, right) => left.name.localeCompare(right.name) || left.region.localeCompare(right.region));
const inventoryByKey = new Map(inventory.map(resource => [resource.key, resource]));
const searchIndex = new Map(inventory.map(resource => [resource.key,
    `${resource.name} ${resource.id} ${resource.instance_type ?? ''} ${(resource.cluster ?? []).join(' ')}`.toLowerCase()]));

function groupBy(rows, keyFor) {
    const groups = new Map();
    for (const row of rows) {
        const key = keyFor(row);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(row);
    }
    return groups;
}
let activeView='overview',activeResourceKey='';
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function append(parent,...children){children.forEach(c=>parent.append(c));return parent;}
function option(select,value,text){const n=el('option',text??value);n.value=value;select.append(n);}
function money(v,c){return v===null||v===undefined?t('unavailable'):`${c} ${amountFormatter.format(Number(v))}`;}
const displayZone=data.context.timezone||'UTC';
const dateFormatter=new Intl.DateTimeFormat('en-GB',{timeZone:displayZone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZoneName:'shortOffset'});
function date(v){const parts=Object.fromEntries(dateFormatter.formatToParts(new Date(v.length===10?v+'T00:00:00Z':v)).map(p=>[p.type,p.value]));const {year:y,month:m,day:d,hour:h,minute:min,timeZoneName:offset}=parts;const formatted=data.context.date_format==='dmy'?`${d}/${m}/${y}`:data.context.date_format==='mdy'?`${m}/${d}/${y}`:`${y}-${m}-${d}`;return `${formatted} ${h}:${min} (${offset})`;}

function monthLabel(month) { return monthLabels.get(month) ?? month; }
function basis(b){return t(b==='estimated'?'estimated_basis':b);}
function badge(b){return el('span',basis(b),`badge ${b}`);}
function table(headers,rows){const table=el('table'), head=el('tr'), body=el('tbody');headers.forEach(h=>head.append(el('th',h)));append(table,append(el('thead'),head),body);rows.forEach(values=>{const tr=el('tr');values.forEach((v,i)=>{const td=el('td',undefined,i===values.length-1?'num':'');td.append(v instanceof Node?v:document.createTextNode(String(v??'—')));tr.append(td);});body.append(tr);});return append(el('div',undefined,'scroll'),table);}
function navigate(key){const r=byKey.get(key);if(!r)return;$('service').value=r.service;$('region').value=r.region;$('search').value='';activeResourceKey=key;activeView='resources';render();$('view-content').scrollIntoView({behavior:'smooth'});}

function resourceLink(r){const a=el('a',r.name);a.href='#'+r.key;a.addEventListener('click',event=>{event.preventDefault();navigate(r.key);});return a;}
function own(entry,currency){return entry.currency===currency?money(entry.amount,currency):t('unavailable');}

function typeLabel(kind){const names={instance:['EC2 instance','Istanza EC2'],volume:['EBS disk','Disco EBS'],security_group:['Security group (SG)','Gruppo di sicurezza (SG)'],vpc:['VPC','VPC'],subnet:['Subnet','Subnet'],cluster:['Cluster','Cluster'],nodegroup:['Node group','Gruppo di nodi'],network_interface:['Network interface','Interfaccia di rete'],resource:['Resource','Risorsa']};return names[kind]?tr(...names[kind]):String(kind).replaceAll('_',' ');}
function matchesSearch(resource, query) {
    return searchIndex.get(resource.key).includes(query);
}
function visibleResources(service, region) {
    const candidates = service ? resourcesByService.get(service) ?? [] : resources;
    return region ? candidates.filter(resource => resource.region === region) : candidates;
}
function billsFor(month, currency, service, region) {
    const names = billingNames.get(service);
    return (billingByMonth.get(`${month}|${currency}`) ?? []).filter(bill =>
        (!region || bill.region === region) &&
        (!service || (service.startsWith('billing:') ? bill.service === service.slice(8) : names?.has(bill.service))));
}
function sums(rows,month,currency){let total=0,estimated=0,known=0;rows.forEach(r=>{const v=r.months[month];if(v.currency===currency&&v.amount!==null){total+=Number(v.amount);estimated+=Number(v.estimated??0);known++;}});return {total,estimated,known};}
function bars(container,entries,currency){container.replaceChildren();const max=Math.max(1,...entries.map(e=>Math.abs(Number(e[1]))));if(!entries.length){container.append(el('p',t('unavailable'),'muted'));return;}entries.forEach(([name,amount])=>{const track=el('div',undefined,'track'),bar=el('span');bar.style.width=`${Math.abs(Number(amount))/max*100}%`;track.append(bar);container.append(append(el('div',undefined,'breakdown'),el('span',name),track,el('span',money(amount,currency),'amount')));});}
function renderCard(r,month,currency){
 const value=r.months[month], total=r.totals[month], card=el('article',undefined,'resource');card.id=r.key;
 const identity=el('div',undefined,'identity');append(identity,badge(total.basis),el('span',r.service.toUpperCase(),'badge'),el('h3',r.name));
 append(identity,el('div',`${r.plan.currency!==currency||r.plan.rate_label==='Rate unavailable'?t('unavailable'):r.plan.rate_label} · ${own(value,currency)} / ${monthLabel(month)} (${basis(value.basis)})`,'rate-title'));
 const specs=[r.id,r.region,r.instance_type,r.storage_type,r.state,r.vcpu!=null?`${r.vcpu} vCPU`:null,r.memory_mib!=null?`${Number(r.memory_mib)/1024} GiB RAM`:null,r.size_gib!=null?`${r.size_gib} GiB`:null].filter(Boolean);
 identity.append(el('div',specs.join(' · '),'muted'));
 const gross=append(el('div',undefined,'resource-total'),el('small',t('attached')),el('strong',money(total.amounts[currency],currency)),el('small',`${monthLabel(month)}${total.complete?'':' · '+t('partial')}`));
 append(card,append(el('div',undefined,'resource-head'),identity,gross));
 const body=el('div',undefined,'resource-body');body.append(el('p',`${t('own')}: ${own(value,currency)}`));
 const connected=r.included_keys.filter(k=>k!==r.key).map(k=>byKey.get(k));
 if(connected.length){const related=el('details');related.open=true;related.append(el('summary',t('attachments')));related.append(table([t('resource'),t('type'),t('source'),t('own')],connected.map(c=>[resourceLink(c),typeLabel(c.type),el('span',`${basis(c.months[month].basis)} · ${t('included')}`,'included-cost'),own(c.months[month],currency)])));body.append(related);}
 if(r.relations?.length){const rel=el('details');rel.open=true;rel.append(el('summary',t('relations')),el('p',t('relations_note')));rel.append(table([t('resource'),t('type'),t('source'),t('own')],r.relations.map(ref=>{
  const target=byKey.get(ref.key),included=target&&r.included_keys.includes(target.key),parent=target&&target.included_keys.includes(r.key);
  const status=included?t('included'):parent?t('included_in_parent'):t('reference');
  return [target?resourceLink(target):ref.name||ref.id,typeLabel(ref.type||'resource'),el('span',status,included||parent?'included-cost':''),target&&(included||parent)?`${own(target.months[month],currency)} · ${basis(target.months[month].basis)}`:'—'];
 })));body.append(rel);}
 body.append(renderCalculation(r,month,currency));card.append(body);return card;
}

function translateNote(note){
 if(language==='en')return note;
 const exact=data.note_translations?.[note]??data.messages?.[note];if(exact)return exact;
 if(note.startsWith('No unambiguous supported unit price'))return tf('missing_price_note',{status:note.split('. ').slice(1).join('. ')});
 if(note.startsWith('Current On-Demand rate, with'))return tf('current_rate_note',{hours:data.assumptions.hours_per_day});
 if(note.startsWith('Assumed ')&&note.includes('support;'))return tf('eks_support_note',{support:data.assumptions.eks_support});
 if(note.startsWith('EKS ')&&note.endsWith('(assumed)'))return tf('eks_component',{support:data.assumptions.eks_support});
 if(note.startsWith('No unique catalog rate'))return t('eks_missing_rate');
 if(note.startsWith('Linked volume '))return tf('missing_volume_note',{id:note.split(' ')[2]});
 return note;
}
function renderOverview(month,service,region,selected,bills,summary){
 const currency='USD';
 $('modeled-total').textContent=summary.known?money(summary.total,currency):t('unavailable');$('estimated-total').textContent=summary.known?money(summary.estimated,currency):t('unavailable');
 $('billed-total').textContent=bills.length?money(bills.reduce((n,b)=>n+Number(b.amount),0),currency):t('unavailable');
 $('coverage-note').textContent=tf('coverage_note',{known:summary.known,total:selected.length});
 const trend=$('trend');trend.replaceChildren();const monthly=data.windows.map(w=>{const s=sums(selected,w.month,currency),b=billsFor(w.month,currency,service,region);return {month:w.month,model:s.known?s.total:null,bill:b.length?b.reduce((n,v)=>n+Number(v.amount),0):null};});
 const maximum=Math.max(1,...monthly.flatMap(v=>[Math.abs(v.model??0),Math.abs(v.bill??0)]));
 monthly.forEach(v=>{const column=el('div',undefined,'column'),pair=el('div',undefined,'bar-pair');[['model',t('model')],['bill',t('billing')]].forEach(([key,label])=>{const wrap=el('div',undefined,'bar-wrap'),bar=el('div',undefined,'bar '+key);bar.style.setProperty('--h',`${v[key]===null?0:Math.abs(v[key])/maximum*105}px`);bar.title=`${label}: ${money(v[key],currency)}`;append(wrap,el('small',money(v[key],currency),'chart-value'),bar);pair.append(wrap);});append(column,pair,el('small',monthLabel(v.month)));trend.append(column);});
 const grouped=new Map(),regional=new Map();
 selected.forEach(r=>{const v=r.months[month];if(v.amount===null||v.currency!==currency)return;const label=service?r.key:r.service.toUpperCase();grouped.set(label,(grouped.get(label)??0)+Number(v.amount));});
 selected.forEach(r=>{const v=r.months[month];if(v.amount!==null&&v.currency===currency)regional.set(r.region,(regional.get(r.region)??0)+Number(v.amount));});
 const sort=values=>[...values].sort((a,b)=>b[1]-a[1]);const entries=sort(grouped).slice(0,10).map(([key,value])=>[service?`${byKey.get(key).name} · ${byKey.get(key).region}`:key,value]);bars($('service-chart'),entries,currency);bars($('region-chart'),sort(regional),currency);$('service-chart-title').textContent=t(service?'chart_resources':'chart_services');
}
function renderStructure(r){const box=el('article',undefined,'panel');box.append(el('h3',r.name||r.id),el('p',`${typeLabel(r.type)} · ${r.region}`,'muted'),el('p',tr('Organizational resource: no own cost allocated.','Struttura organizzativa: nessun costo proprio attribuito.')));if(r.relations?.length)box.append(table([t('resource'),t('type'),t('source')],r.relations.map(ref=>[ref.key?resourceLink(byKey.get(ref.key)):ref.name||ref.id,typeLabel(ref.type),t('reference')])));return box;}
function renderResources(month,service,region){
 const query=$('search').value.toLowerCase();
 const matches=inventory.filter(resource=>(!service||resource.service===service)&&(!region||resource.region===region)&&matchesSearch(resource,query));
 $('result-count').textContent=String(matches.length);
 const host=$('resources');host.replaceChildren();
 if(!matches.length){host.append(el('p',t('empty'),'empty'));return;}
 const label=el('label',t('select_resource')),select=el('select',undefined,'resource-selector');select.id='resource-choice';label.append(select);host.append(label);
 matches.forEach(r=>option(select,r.key,`${r.name||r.id} · ${typeLabel(r.type)} · ${r.region}`));
 if(!matches.some(r=>r.key===activeResourceKey))activeResourceKey=matches[0].key;
 select.value=activeResourceKey;
 const detail=el('div');host.append(detail);
 const show=()=>{activeResourceKey=select.value;const chosen=inventoryByKey.get(activeResourceKey);detail.replaceChildren(chosen.months?renderCard(chosen,month,'USD'):renderStructure(chosen));};
 select.addEventListener('change',show);show();
}
function renderBilling(month,service,bills){
 $('shared-billing').textContent=['ebs','vpc'].includes(service)?t('shared_billing'):'';
 const host=$('billing-table');
 if(!bills.length){host.append(el('p',t('no_billing'),'muted'));return;}
 const rows=bills.map(b=>[b.service,b.region,b.metric+(b.estimated?(tr(' · provisional',' · provvisorio')):''),money(b.amount,'USD')]);
 rows.push([tr('Monthly total','Totale del mese'),monthLabel(month),'',money(bills.reduce((n,b)=>n+Number(b.amount),0),'USD')]);
 host.append(table([t('service'),t('region'),t('metric'),t('aws_cost')],rows));host.querySelector('tbody tr:last-child').classList.add('billing-total');
}
function renderExports(service){
 const select=$('export-service');serviceKeys.forEach(key=>option(select,key,key.startsWith('billing:')?key.slice(8):key.toUpperCase()));
 if(data.workbooks[service])select.value=service;
 $('export-resources').disabled=!select.options.length;
 $('export-resources').addEventListener('click',()=>downloadWorkbook(select.value,$('export-style').value));
}
function render() {
    const month = $('month').value;
    const service = $('service').value;
    const region = $('region').value;
    const window = windowsByMonth.get(month);
    const scope = service ? (service.startsWith('billing:') ? service.slice(8) : service.toUpperCase()) + ' · ' : '';
    $('scope-title').textContent = scope + t(activeView);
    $('period-note').textContent = `${date(window.start)} → ${date(window.end)} (${tr('exclusive end','fine esclusa')}) · ${displayZone} · ${tr('billing UTC','fatturazione UTC')}`;
    $('service-nav').querySelectorAll('button').forEach(button => {
        const active = button.dataset.service === service;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
    });
    $('section-nav').querySelectorAll('button').forEach(button => {
        const active = button.dataset.view === activeView;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
    });
    $('search').disabled = activeView !== 'resources';
    $('view-content').replaceChildren($('view-' + activeView).content.cloneNode(true));
    switch (activeView) {
        case 'overview': {
            const selected = visibleResources(service, region);
            renderOverview(month, service, region, selected, billsFor(month, 'USD', service, region), sums(selected, month, 'USD'));
            break;
        }
        case 'resources': renderResources(month, service, region); break;
        case 'billing': renderBilling(month, service, billsFor(month, 'USD', service, region)); break;
        case 'exports': renderExports(service); break;
    }
}
data.windows.forEach(w=>option($('month'),w.month,monthLabel(w.month)));$('month').value=data.windows.at(-1).month;
serviceKeys.forEach(s=>option($('service'),s,s.startsWith('billing:')?s.slice(8):s.toUpperCase()));
['',...serviceKeys].forEach(service=>{const b=el('button',service?service.startsWith('billing:')?service.slice(8):service.toUpperCase():t('all_services'));b.dataset.service=service;b.addEventListener('click',()=>{$('service').value=service;activeResourceKey='';render();});$('service-nav').append(b);});
['overview','resources','billing','exports'].forEach(view=>{const b=el('button',t(view));b.dataset.view=view;b.addEventListener('click',()=>{activeView=view;render();});$('section-nav').append(b);});
[...new Set([...resources.map(r=>r.region),...data.billing.map(b=>b.region)])].filter(Boolean).sort().forEach(r=>option($('region'),r));
['month','service','region'].forEach(id=>$(id).addEventListener('change',()=>{activeResourceKey='';render();}));
$('search').addEventListener('input',()=>{activeResourceKey='';render();});
function theme(value){document.documentElement.dataset.theme=value;$('theme').setAttribute('aria-checked',String(value==='dark'));try{localStorage.setItem('aws-inventory-theme',value);}catch{}}
let initial=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';try{initial=localStorage.getItem('aws-inventory-theme')||initial;}catch{}theme(initial==='dark'?'dark':'light');
$('theme').addEventListener('click',()=>theme(document.documentElement.dataset.theme==='dark'?'light':'dark'));
render();
