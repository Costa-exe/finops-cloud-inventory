/* Download the embedded per-service Excel workbook without network access. */
function downloadWorkbook(service,style='readable'){
 const status=$('export-status'),workbook=data.workbooks[service]?.[style];if(!workbook)return;
 try{const bytes=Uint8Array.from(atob(workbook.base64),c=>c.charCodeAt(0)),url=URL.createObjectURL(new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})),link=document.createElement('a');
 link.href=url;link.download=workbook.filename;link.hidden=true;
 try{document.body.append(link);link.click();status.textContent=`${t('export_done')}: ${workbook.filename}`;}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}
 }catch{status.textContent=t('export_error');}
}
