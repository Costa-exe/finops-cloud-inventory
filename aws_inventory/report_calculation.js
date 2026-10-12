/* Explain the estimator's stored amounts; do not change cost allocation. */
function renderCalculation(resource,month,currency){
 const value=resource.months[month],plan=resource.plan,window=windowsByMonth.get(month);
 const box=el('details',undefined,'calculation');box.append(el('summary',t('calculation')));
 const number=v=>v===null||v===undefined?'—':quantityFormatter.format(Number(v));
 const usd=value.currency===currency,canEstimate=usd&&value.estimated!==null&&value.estimated!==undefined;
 const explanations={
  billed:tr('The own cost comes from AWS resource billing for the returned intervals. Catalog prices do not replace those amounts.', 'Il costo proprio proviene dalla fatturazione AWS della risorsa per gli intervalli restituiti. I prezzi di catalogo non sostituiscono questi importi.'),
  estimated:tr('No direct resource billing covers these intervals. The cost is a scenario based on current capacity, supported catalog prices and configured runtime.', 'Questi intervalli non sono coperti da fatturazione diretta della risorsa. Il costo è uno scenario basato su capacità attuale, tariffe supportate e durata di utilizzo impostata.'),
  mixed:tr('Returned resource billing is used where available. Only the uncovered intervals are estimated, avoiding overlap.', 'Si usa la fatturazione della risorsa dove disponibile. La stima copre solo gli intervalli scoperti, evitando sovrapposizioni.'),
  unavailable:tr('A resource cost cannot be calculated from the available billing, supported prices and quantities. Missing amounts are not zero.', 'I dati disponibili di fatturazione, tariffe supportate e quantità non permettono di calcolare un costo della risorsa. Un importo mancante non equivale a zero.')
 };
 box.append(el('p',explanations[usd?value.basis:'unavailable']??explanations.unavailable));
 if(data.simulation_only)box.append(el('p',tr('Offline simulation: both prices and billed amounts are fictional.', 'Simulazione offline: sia le tariffe sia gli importi fatturati sono fittizi.')));
 const facts=el('dl',undefined,'calculation-facts');
 const fact=(label,text)=>facts.append(append(el('div'),el('dt',label),el('dd',text)));
 fact(tr('Selected interval (exclusive end)', 'Intervallo selezionato (fine esclusa)'),`${date(window.start)} → ${date(window.end)} · ${displayZone}`);
 fact(tr('Calendar hours / full month', 'Ore di calendario / mese intero'),`${number(window.hours)} / ${number(window.month_hours)}`);
 fact(tr('Hours covered by resource billing', 'Ore coperte da fatturazione della risorsa'),number(value.covered_hours));
 fact(tr('Uncovered calendar hours', 'Ore di calendario non coperte'),number(value.modeled_hours));
 fact(tr('Own billed amount', 'Quota propria fatturata'),usd?money(value.billed,currency):t('unavailable'));
 fact(tr('Own estimated amount', 'Quota propria stimata'),usd?money(value.estimated,currency):t('unavailable'));
 box.append(facts);
 if(value.aws_provisional)box.append(el('p',tr('AWS marks the returned billed amount as provisional.', 'AWS indica come provvisorio l’importo fatturato restituito.')));
 box.append(el('p',tr('Own cost = returned resource billing + supported estimates for uncovered intervals. Billing and estimates are never added for the same hours.', 'Costo proprio = fatturazione della risorsa restituita da AWS + stime supportate per gli intervalli scoperti. Fatturazione e stima non vengono sommate per le stesse ore.'),'calculation-formula'));
 if(plan.currency===currency&&plan.components.length){
  const rows=plan.components.map(c=>{
   const hours=Number(value.modeled_hours??0),divisor=c.basis==='monthly'?Number(window.month_hours):1;
   const formula=canEstimate?`${c.rate} ${currency} × ${number(c.quantity)} × ${number(hours)} h${c.basis==='monthly'?` / ${number(divisor)} h`:''}`:tr('Catalog rate available; not applied to this month', 'Tariffa disponibile; non applicata a questo mese');
   const result=canEstimate?money(Number(c.rate)*Number(c.quantity)*hours/divisor,currency):'—';
   const source=el('span',`${translateNote(c.label)} · ${c.rate} ${currency}/${c.unit}`);
   if(c.effective_date)source.append(el('small',tr(' · effective from ', ' · valida dal ')+String(c.effective_date)));
   return [source,formula,result];
  });
  box.append(table([t('component'),tr('Calculation for uncovered hours', 'Calcolo sulle ore scoperte'),tr('Estimated component', 'Componente stimata')],rows));
  box.append(el('p',tr('Hourly components: unit rate × quantity/runtime factor × uncovered hours. Monthly components: unit rate × quantity × uncovered hours ÷ calendar hours in the full month. Component results are displayed rounded; the stored own cost is authoritative.', 'Componenti orarie: tariffa × quantità/fattore di utilizzo × ore scoperte. Componenti mensili: tariffa × quantità × ore scoperte ÷ ore di calendario del mese intero. Le componenti sono mostrate arrotondate; il costo proprio usa gli importi calcolati dal modello.')));
 }
 box.append(el('p',tf('runtime_note',{hours:data.assumptions.hours_per_day})));
 const included=resource.included_keys.filter(k=>k!==resource.key).length;
 box.append(el('p',tf('subtotal_note',{count:included})));
 box.append(el('p',tr('Sources: AWS Cost Explorer for returned resource costs; AWS Price List for catalog estimates. Applying current inventory/prices to past months does not reconstruct historical runtime, scaling, Spot prices or discounts. Usage charges without measured consumption remain excluded.', 'Fonti: AWS Cost Explorer per i costi della risorsa restituiti; AWS Price List per le stime di catalogo. Applicare inventario e prezzi attuali a mesi passati non ricostruisce accensioni, scalabilità, prezzi Spot o sconti storici. I costi a consumo senza misurazioni restano esclusi.')));
 const notes=el('ul');[...plan.notes,...resource.notes].forEach(note=>notes.append(el('li',translateNote(note))));box.append(notes);
 return box;
}
