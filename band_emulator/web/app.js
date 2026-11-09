'use strict';
const $ = id => document.getElementById(id);
const hash = new URLSearchParams(location.hash.slice(1));
let key = hash.get('key') || sessionStorage.getItem('band-key') || '';
if (hash.has('key')) { sessionStorage.setItem('band-key', key); history.replaceState(null, '', location.pathname); }
let state = null, version = -1, busy = false, connected = false;
addEventListener('hashchange',()=>{const next=new URLSearchParams(location.hash.slice(1)).get('key');if(next){key=next;sessionStorage.setItem('band-key',key);history.replaceState(null,'',location.pathname);version=-1;refresh();}});
const stageNames = {internal_runtime_relocation:'Перенос кода в SRAM',external_runtime_relocation:'Инициализация PSRAM',c_runtime_entry:'Запуск C runtime',kernel_entry:'Вход в ядро ОС',system_heaps_initialized:'Системные кучи созданы',kernel_exception_handlers_configured:'Обработчики исключений',system_clock_initialized:'Системный таймер',task_trampoline:'Переключение контекста',bringup_task_entry:'Задача AppBringUp',board_gpio_configured:'Настройка GPIO',board_adc_devices_registered:'Регистрация устройств АЦП',flash_geometry_verified:'Проверка геометрии памяти',board_storage_init_returned:'Регистрация разделов',bluetooth_driver_init_entry:'Старт драйвера Bluetooth',board_init_returned:'Инициализация платы завершена'};
function note(text, error=false) { $('notice').textContent=text; $('notice').classList.toggle('error',error); }
function node(tag,text,cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
async function api(path, body) {
  const response = await fetch(path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+key,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(15000)});
  if(response.status===401) { $('login').classList.remove('hidden'); throw new Error('Введите ключ доступа или откройте ссылку подключения'); }
  if(!response.ok){ const r=await response.json(); throw new Error(r.error||'Ошибка сервера'); }
  return response;
}
function connection(ok) { if(ok&&!connected)note('Соединение установлено · готов к работе'); connected=ok; $('led').classList.toggle('live',ok); $('connection').textContent=ok?'Сервер подключён':'Нет подключения'; document.querySelectorAll('.toolbar button').forEach(b=>b.disabled=busy||!ok); }
async function refresh() {
  if(!key){$('login').classList.remove('hidden');connection(false);return;}
  try { const r=await(await api('/api/state')).json(); connection(true); $('login').classList.add('hidden'); if(r.error)throw new Error(r.error); if(!r.ready){note('Подготовка машины…');return;} if(r.version>version){version=r.version;render(r);} }
  catch(e){connection(false);note(e.message,true);}
}
async function command(action,extra={}) {
  if(busy)return;
  busy=true; connection(connected);
  try { const r=await(await api('/api/command',{action,...extra})).json(); if(r.result&&r.result.hex!==undefined)renderMemory(r.result); await refresh(); note(action==='run'?'Выполнение продолжается на сервере':action==='reset'?'Машина сброшена. Снимки удалены.':'Команда выполнена'); }
  catch(e){note(e.message,true);} finally{busy=false;connection(connected);}
}
function render(r){
  state=r;
  $('instructions').textContent=r.instructions.toLocaleString('ru-RU'); $('pc').textContent=r.registers.pc;
  $('taskCount').textContent=r.tasks.length; $('exceptions').textContent=r.exception_count;
  $('status').textContent=r.running?'● Выполняется':r.fault?'Аппаратная остановка':r.status==='breakpoint'?'Точка останова':'На паузе';
  $('status').className='badge'+(r.running?' running':r.fault?' error':'');
  $('fault').classList.toggle('hidden',!r.fault); $('faultDetails').textContent=r.fault?Object.entries(r.fault).map(([k,v])=>`${k}: ${typeof v==='object'?JSON.stringify(v):v}`).join('  ·  '):'';
  $('tasks').replaceChildren(...r.tasks.map(t=>{const row=node('tr');for(const k of ['pid','name','state','entry'])row.append(node('td',t[k]));return row;}));
  if(!r.tasks.length){const row=node('tr'),cell=node('td','Задачи появятся после запуска ядра');cell.colSpan=4;row.append(cell);$('tasks').append(row);}
  $('milestones').replaceChildren(...r.milestones.map(m=>{const li=node('li');li.append(node('span',stageNames[m.stage]||m.stage),node('small',m.instructions.toLocaleString('ru-RU')));return li;}));
  if(!r.milestones.length)$('milestones').append(node('li','Готов к запуску оригинального образа'));
  $('console').textContent=r.console||'Вывод прошивки появится после запуска C runtime.';
  $('registers').replaceChildren(...Object.entries(r.registers).map(([k,v])=>{const el=node('div');el.append(node('span',k),node('b',v));return el;}));
  $('trace').textContent=r.trace.length?r.trace.map(t=>`${t.address.padEnd(12)} ${t.instruction}`).join('\n'):(r.next||[]).length?'Трасса пишется только при пошаговом исполнении. Следующие инструкции от PC:\n'+r.next.map(t=>`${t.address.padEnd(12)} ${t.instruction}`).join('\n'):'Инструкции ещё не исполнялись.';
  $('breakpoints').replaceChildren(...r.breakpoints.map(address=>{const b=node('button',address+' ×');b.title='Удалить точку останова';b.onclick=()=>command('remove_breakpoint',{address});return b;}));
  $('snapshots').replaceChildren(...r.snapshots.map(name=>{const el=node('div'),restore=node('button','Восстановить'),remove=node('button','×');restore.onclick=()=>command('restore',{name});remove.title='Удалить снимок';remove.onclick=()=>command('delete_snapshot',{name});el.append(node('span',name),restore,remove);return el;}));
  $('gpioState').textContent=Object.entries(r.gpio.banks).map(([addr,b])=>`${addr}\nВходы ${b.inputs}   Выходы ${b.output}\nНаправление ${b.direction}   IRQ enabled ${b.enabled}\nОжидают ${b.pending}   Маска ${b.masked}\n`).join('\n')+`\nIRQ ${r.gpio.irq}: ${r.gpio.asserted?'активен':'не активен'}`;
  $('adcState').textContent=Object.entries(r.virtual_adc_inputs).map(([k,v])=>`Канал ${k} → ${v}`).join('\n');
  $('clockState').textContent=`Частотный профиль: ${r.virtual_clock.clock_profile}\nПрофиль питания: ${r.virtual_clock.power_profile}\nЗначения 0–7; физические частоты не моделируются.`;
  $('services').textContent=JSON.stringify(r.services,null,2); $('limitations').textContent=r.limitations.join('\n')+'\n\n'+JSON.stringify(r.virtual_factory_sector,null,2);
  if(r.notice)note(r.notice);
}
function renderMemory(r){const bytes=r.hex.match(/../g)||[],base=parseInt(r.address,16),lines=[];for(let i=0;i<bytes.length;i+=16){const part=bytes.slice(i,i+16),ascii=part.map(x=>{const n=parseInt(x,16);return n>=32&&n<127?String.fromCharCode(n):'.';}).join('');lines.push((base+i).toString(16).padStart(8,'0')+'  '+part.join(' ').padEnd(47)+'  '+ascii);}$('memory').textContent=`${r.size} байт · инструкция ${r.instructions}\n\n`+lines.join('\n');}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(t=>t.classList.toggle('active',t===b));document.querySelectorAll('.tab-content').forEach(t=>t.classList.toggle('hidden',t.id!==b.dataset.tab));});
document.querySelectorAll('[data-command]').forEach(b=>b.onclick=()=>command(b.dataset.command,b.dataset.command==='step'?{count:$('steps').value}:{}));
document.querySelectorAll('[data-address]').forEach(b=>b.onclick=()=>{$('bpAddress').value=b.dataset.address;command('breakpoint',{address:b.dataset.address});});
$('connect').onclick=()=>{key=$('key').value.trim();sessionStorage.setItem('band-key',key);version=-1;refresh();};
$('key').onkeydown=e=>{if(e.key==='Enter')$('connect').click();};
$('share').onclick=async()=>{try{const r=await(await api('/api/connect')).json();$('shareAddress').replaceChildren(...r.addresses.map(a=>{const o=node('option',a);o.value=a;return o;}));const lan=r.addresses.find(a=>a.startsWith('http://192.168.'))||r.addresses.find(a=>!a.includes('127.0.0.1'))||r.addresses[0];$('shareAddress').value=lan;$('sharePanel').classList.remove('hidden');$('shareAddress').onchange();}catch(e){note(e.message,true);}};
$('shareAddress').onchange=()=>{$('shareLink').value=$('shareAddress').value+'#key='+key;};
$('closeShare').onclick=()=>$('sharePanel').classList.add('hidden');
$('reset').onclick=()=>{if(confirm('Сбросить общую машину? Снимки и изменения RAM будут удалены у всех подключённых устройств.'))command('reset');};
$('addBreakpoint').onclick=()=>command('breakpoint',{address:$('bpAddress').value});
$('save').onclick=()=>command('save',{name:$('snapshotName').value});
for(let i=0;i<13;i++)$('registerName').append(node('option','r'+i));for(const n of ['sp','lr','pc'])$('registerName').append(node('option',n));
$('writeRegister').onclick=()=>command('register',{name:$('registerName').value,value:$('registerValue').value});
$('readMemory').onclick=()=>command('read_memory',{address:$('memAddress').value,size:$('memSize').value});
$('writeMemory').onclick=()=>command('write_memory',{address:$('memAddress').value,hex:$('memHex').value});
$('gpioHigh').onclick=()=>command('gpio',{pin:$('gpioPin').value,level:1});$('gpioLow').onclick=()=>command('gpio',{pin:$('gpioPin').value,level:0});
$('setAdc').onclick=()=>command('adc',{channel:$('adcChannel').value,value:$('adcValue').value});
$('download').onclick=async()=>{try{const blob=await(await api('/api/report')).blob(),url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download='band-debug-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){note(e.message,true);}};
async function poll(){await refresh();setTimeout(poll,document.hidden?2000:600);}poll();
