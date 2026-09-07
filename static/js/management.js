(() => {
  const request = async (url,options={}) => {
    const response=await fetch(url,{credentials:'same-origin',...options});
    let data;try{data=await response.json();}catch{throw Error('服务暂时没有返回有效结果，请稍后重试');}
    if(!response.ok)throw Error(data.error||'请求失败');return data;
  };
  document.querySelectorAll('select[data-reference]').forEach(select=>{
    const form=select.closest('form');const search=form.querySelector(`[data-for="${select.name}"]`);let seq=0;
    const load=async()=>{const index=++seq;try{
      const params=new URLSearchParams({q:search?.value||'',purpose:select.name,worker_permission:form.dataset.businessCommand.startsWith('complaint.')?'complaint.handle':'inspection.write'});
      const data=await request(`/api/options/${select.dataset.reference}?${params}`);if(index!==seq)return;
      const selected=new Set([...select.selectedOptions].map(o=>o.value));const retained=[...select.options].filter(o=>selected.has(o.value)&&o.value&&!data.items.some(x=>String(x.id)===o.value));
      select.replaceChildren(new Option('请选择',''),...retained);
      for(const item of data.items){const option=new Option(item.label,String(item.id));option.selected=selected.has(String(item.id));select.add(option);}
    }catch(error){if(search)search.placeholder=error.message;}};
    if(form.dataset.businessCommand==='payment.record' && select.name==='bill_id')select.addEventListener('change',async()=>{
      const checkbox=form.querySelector('[name=confirmed]');if(checkbox)checkbox.checked=false;
      if(!select.value)return;
      try{const data=await request('/api/manage/bills/'+encodeURIComponent(select.value));let version=form.querySelector('[name=version]');if(!version){version=document.createElement('input');version.type='hidden';version.name='version';form.appendChild(version);}
        version.value=data.record.version;form.querySelector('[name=amount]').value=((data.record.amount_cents-data.record.paid_cents)/100).toFixed(2);
      }catch(e){const error=form.querySelector('.form-error');error.textContent=e.message;error.hidden=false;}
    });
    let timer;search?.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(load,250);});load();
  });
  document.querySelectorAll('.business-form').forEach(form=>form.addEventListener('submit',async event=>{
    event.preventDefault();const button=form.querySelector('button[type=submit]');if(button.disabled)return;
    const error=form.querySelector('.form-error');error.hidden=true;button.disabled=true;
    const fields=new FormData(form),data={};for(const [key,value] of fields){if(!['csrf_token','request_key','confirmed'].includes(key)&&value!=='')data[key]=value;}
    for(const select of form.querySelectorAll('select[multiple]'))data[select.name]=[...select.selectedOptions].map(o=>o.value).filter(Boolean);
    try{const result=await request(`/api/business/${form.dataset.businessCommand}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':fields.get('csrf_token'),'Idempotency-Key':fields.get('request_key')},body:JSON.stringify({data,confirmed:fields.get('confirmed')==='1'})});
      window.location.assign(result.url||'/dashboard');
    }catch(e){error.textContent=e.message;error.hidden=false;button.disabled=false;}
  }));
  document.querySelectorAll('[data-report]').forEach(async node=>{try{const data=await request(node.dataset.report);
    if('receivable_cents' in data)node.textContent=`应收 ¥${(data.receivable_cents/100).toFixed(2)} · 已收 ¥${(data.paid_cents/100).toFixed(2)} · 待收 ¥${(data.unpaid_cents/100).toFixed(2)}`;
    else node.textContent=`${data.month} 投诉分布：`+(data.items.map(x=>`楼栋 #${x.building_id}：${x.count} 件`).join('；')||'本月暂无投诉');
  }catch(e){node.textContent=e.message;}});
})();
