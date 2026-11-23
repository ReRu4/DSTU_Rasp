/* DSTU Mini App. All schedule data comes from the authenticated same-origin API. */
(() => {
  'use strict';
  const app = document.getElementById('app');
  const view = document.getElementById('view');
  const navigation = document.getElementById('navigation');
  const status = document.getElementById('status');
  const telegram = window.Telegram?.WebApp;
  const initData = telegram?.initData || '';
  const weekNames = ['Пн','Вт','Ср','Чт','Пт','Сб'];
  const fullWeekNames = ['Воскресенье','Понедельник','Вторник','Среда','Четверг','Пятница','Суббота'];
  const types = {lec:'Лекция',prac:'Практика',lab:'Лабораторная',other:'Занятие'};
  const statuses = {added:'Добавлено',room:'Аудитория изменена',removed:'Удалено',changed:'Изменено'};
  const state = {
    page:'schedule', mode:'day', date:'', group:'', bootstrap:null, schedule:null,
    detail:null, teacher:null, teacherChoice:'', events:[], nextOffset:null, nextCursor:null, totalEvents:null,
    returnPage:'schedule', lessonId:'', detailTab:'details', sourceDetail:null, localDetail:false, busy:false, error:null,
    settingsTab:'general', settingsOpen:{delivery:true}, groupDrafts:{primary:'',add:''}, settingsFocus:null, feedRevision:null, feedLoadedOlder:false,
    monthDays:{}
  };
  let requestVersion=0, tapTimer=null, lastTap=null, statusTimer=null, syncing=false;
  const scheduleCache=new Map();
  let rosterVersion='', cacheEpoch=0, gesture=null, suppressClickUntil=0, pageMotion=null;
  let scheduleShell=null, feedShell=null;
  const scrollPositions=new Map();
  function clearScheduleCache() {scheduleCache.clear();cacheEpoch++;}
  const esc = value => String(value??'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const attr = esc;
  const btn=(action,value,text,cls='action')=>`<button type="button" class="${cls}" data-${action}="${attr(value)}">${text}</button>`;
  // Lucide outlines; see LICENSE.icons for the ISC notice.
  const iconPaths={
    chevron:'<path d="m9 18 6-6-6-6"/>',
    down:'<path d="m6 9 6 6 6-6"/>',
    refresh:'<path d="M3 12a9 9 0 0 1 15.36-6.36L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.36 6.36L3 16m5 0H3v5"/>'
  };
  const icon=(name,cls='')=>`<svg class="icon ${cls}" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name]||''}</svg>`;
  const parseDate=value=>new Date(value+'T12:00:00Z');
  const isoDate=date=>date.toISOString().slice(0,10);
  const shiftDay=(value,amount)=>{const d=parseDate(value);d.setUTCDate(d.getUTCDate()+amount);return isoDate(d);};
  const monday=value=>shiftDay(value,-((parseDate(value).getUTCDay()+6)%7));
  const firstMonth=value=>value.slice(0,7)+'-01';
  const shiftMonth=(value,amount)=>{const d=parseDate(firstMonth(value));d.setUTCMonth(d.getUTCMonth()+amount);return isoDate(d);};
  const lastMonth=value=>shiftDay(shiftMonth(value,1),-1);
  const safeDay=value=>parseDate(value).getUTCDay()===0?shiftDay(value,1):value;
  const minutes=value=>{const [h,m]=String(value).split(':').map(Number);return h*60+m;};
  const formatDate=(value,options={day:'numeric',month:'long'})=>parseDate(value).toLocaleDateString('ru-RU',{...options,timeZone:'UTC'});
  const stamp=value=>{
    if(!value) return 'Время проверки неизвестно';
    const date=new Date(value);
    return Number.isNaN(date.valueOf())?'Время проверки неизвестно':date.toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'});
  };
  const initials=name=>String(name||'').split(/\s+/).filter(Boolean).slice(0,2).map(x=>x[0]).join('').toUpperCase();
  const activeGroups=()=>state.bootstrap?.groups.filter(group=>group.primary || group.enabled) || [];
  const profile=()=>state.bootstrap?.profile || {};
  function notify(message,timeout=5000) {
    clearTimeout(statusTimer); status.textContent=message; status.hidden=false;
    if(timeout) statusTimer=setTimeout(()=>status.hidden=true,timeout);
  }
  function syncTheme() {
    const scheme=telegram?.colorScheme;
    document.documentElement.style.colorScheme=scheme==='dark'?'dark':'light';
    try { telegram?.setHeaderColor?.('bg_color'); telegram?.setBackgroundColor?.('bg_color'); } catch(_) {}
  }
  function season() {
    if(!profile().seasonal_theme) return 'classic';
    const month=Number((state.date || state.bootstrap?.today || '').slice(5,7));
    return month>=9&&month<=11?'autumn':month===12||month<=2?'winter':month<=5?'spring':'summer';
  }
  function setBusy(value) {
    state.busy=value; view.setAttribute('aria-busy',String(value));
    view.querySelectorAll('[data-save],[data-subject],[data-alert],[data-remove-group],[data-group-form] input,button[type="submit"]').forEach(el=>el.disabled=value);
  }
  async function api(path,body) {
    const response=await fetch(path,{
      method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',
      headers:{Authorization:'tma '+initData,...(body===undefined?{}:{'Content-Type':'application/json'})},
      body:body===undefined?undefined:JSON.stringify(body)
    });
    let result;
    try {result=await response.json();} catch(_) {throw new Error('Сервер вернул неверный ответ. Попробуйте ещё раз.');}
    if(!response.ok) {
      const error=new Error(result?.error?.message || 'Не удалось выполнить запрос');
      error.code=result?.error?.code;error.status=response.status;throw error;
    }
    return result;
  }
  function range() {
    if(state.mode==='month') return {from:firstMonth(state.date),to:lastMonth(state.date)};
    const start=monday(state.date);
    return {from:start,to:shiftDay(start,5)};
  }
  function header(title) {
    return `<header class="header"><h1>${esc(title)}</h1></header>`;
  }
  function dateStrip() {
    const start=monday(state.date);
    return `<div class="day-controls"><button type="button" data-shift="-1" class="day-arrow" aria-label="Предыдущий день">${icon('chevron','icon-previous')}</button><div class="dates" aria-label="Дни недели">${weekNames.map((name,i)=>{
      const date=shiftDay(start,i);
      return `<button type="button" data-day="${date}" aria-pressed="${date===state.date}" aria-label="${attr(formatDate(date))}"><small>${name}</small><span>${Number(date.slice(-2))}</span></button>`;
    }).join('')}</div><button type="button" data-shift="1" class="day-arrow" aria-label="Следующий день">${icon('chevron')}</button></div>`;
  }
  function scheduleViewport(content) {
    return `<div class="schedule-viewport" ${state.mode==='day'?'data-swipe-days="true"':''}><div class="schedule-content">${content}</div></div>`;
  }
  function lessonCard(item,detail=false) {
    const tag=detail?'article':'button';
    const attrs=detail?'':`type="button" data-lesson="${attr(item.id)}" aria-label="${attr(item.start+'–'+item.end+', '+item.subject+', аудитория '+item.room)}"`;
    const statusName=item.deleted?'removed':item.status;
    const kind=Object.hasOwn(types,item.type)?item.type:'other';
    const slots=(state.page==='teacher'?state.teacher?.slots:state.schedule?.slots)||state.schedule?.slots||[];
    const slot=slots.findIndex(value=>value.start===item.start&&value.end===item.end);
    return `<${tag} ${attrs} class="lesson" data-kind="${kind}" data-status="${attr(statusName||'')}"><span class="lesson-top"><span class="lesson-time-row"><span class="time">${esc(item.start)}–${esc(item.end)}</span><span class="lesson-meta">${slot>=0?`${slot+1}-я · `:''}${types[kind]}${item.subgroup?' · п/г '+esc(item.subgroup):''}</span></span>${statuses[statusName]?`<span class="lesson-status">${statuses[statusName]}</span>`:''}</span><span class="subject">${esc(item.subject)}</span><span class="lesson-bottom"><span class="teacher-name">${esc(item.teacher||'Преподаватель не указан')}</span><span class="room">${esc(item.room||'—')}</span></span></${tag}>`;
  }
  function agenda(list) {
    if(!list.length) return '<div class="empty">Нет занятий</div>';
    const sorted=[...list].sort((a,b)=>a.start.localeCompare(b.start)||a.end.localeCompare(b.end)||String(a.subgroup).localeCompare(String(b.subgroup))||a.subject.localeCompare(b.subject));
    const active=sorted.filter(item=>!item.deleted);
    if(active.length && active.every(item=>item.military)) return `<details><summary class="military">Военная кафедра</summary><div class="military-list agenda">${sorted.map(item=>lessonCard(item)).join('')}</div></details>`;
    const slots=state.schedule?.slots || [];
    const earliest=active.length?Math.min(...active.map(item=>minutes(item.start))):0;
    const latest=active.length?Math.max(...active.map(item=>minutes(item.end))):0;
    const rows=sorted.map(item=>({time:minutes(item.start),lesson:item}));
    for(const slot of slots) {
      const start=minutes(slot.start),end=minutes(slot.end);
      if(start>=earliest && end<=latest && !active.some(item=>minutes(item.start)<end && minutes(item.end)>start)) rows.push({time:start,slot});
    }
    rows.sort((a,b)=>a.time-b.time);
    return `<div class="agenda">${rows.map(row=>row.lesson?lessonCard(row.lesson):`<div class="window">${esc(row.slot.start)}–${esc(row.slot.end)} · Окно</div>`).join('')}</div>`;
  }
  function daysForMode() {
    if(state.mode==='day') return [state.date];
    const start=monday(state.date);
    return Array.from({length:6},(_,i)=>shiftDay(start,i));
  }
  function teacherAgenda(list) {
    if(!list.length) return '<div class="empty">Нет занятий</div>';
    return `<div class="agenda">${[...list].sort((a,b)=>a.start.localeCompare(b.start)||String(a.group_name||'').localeCompare(String(b.group_name||''))).map(item=>`<div><p class="meta teacher-group-label">${esc(item.group_name)}</p>${lessonCard(item)}</div>`).join('')}</div>`;
  }
  function scheduleBody(lessons,teacher=false) {
    if(state.mode==='month') return monthCalendar(lessons);
    return `<div class="${state.mode==='week'?'week-days':''}">${daysForMode().map(date=>{
      const dayLessons=lessons.filter(item=>item.date===date);
      const groups=teacher?[{id:'teacher',name:''}]:state.group==='all'?activeGroups():activeGroups().filter(group=>String(group.id)===state.group);
      return `<section class="day-section"><div class="heading ${state.mode==='day'?'day-heading':''}"><h2>${esc(fullWeekNames[parseDate(date).getUTCDay()]+', '+formatDate(date))}</h2></div>${groups.map((group,index)=>{
        const filtered=teacher?dayLessons:dayLessons.filter(item=>String(item.group_id)===String(group.id));
        if(!teacher && state.group==='all') return groupBlock(group,filtered,index===0,date);
        if(teacher) return teacherAgenda(filtered);
        return agenda(filtered);
      }).join('')}</section>`;
    }).join('')}</div>`;
  }
  function groupBlock(group,list,open=false,date=state.date) {
    const active=list.filter(item=>!item.deleted);
    const changed=list.filter(item=>item.status && item.status!=='baseline').length;
    const sorted=[...active].sort((a,b)=>a.start.localeCompare(b.start));
    const meta=active.length?active.every(item=>item.military)?'Военная кафедра':`${active.length} зан. · ${sorted[0].start}–${sorted.at(-1).end}`:'Нет занятий';
    return `<details class="group-block" data-block-key="${attr(group.id+':'+date)}" ${open?'open':''}><summary class="group-summary"><span><strong>${esc(group.name)}</strong><small>${esc(meta)}${changed?' · '+changed+' изм.':''}</small></span>${icon('down','group-chevron')}</summary><div class="group-body">${agenda(list)}</div></details>`;
  }
  function occupiedSlots(list) {
    const slots=new Map();
    for(const item of list) {
      if(item.deleted) continue;
      const key=item.start+'–'+item.end;
      if(!slots.has(key)) slots.set(key,{start:item.start,end:item.end,kinds:new Set()});
      slots.get(key).kinds.add(Object.hasOwn(types,item.type)?item.type:'other');
    }
    return [...slots.values()].sort((a,b)=>a.start.localeCompare(b.start)||a.end.localeCompare(b.end));
  }
  function pairCount(count) {
    const last=count%10,teen=count%100;
    return `${count} ${teen>=11&&teen<=14?'пар':last===1?'пара':last>=2&&last<=4?'пары':'пар'}`;
  }
  function monthSelectionKey(groupId,date=state.date) {
    return `${state.page==='teacher'?'teacher-'+(state.teacher?.teacher?.id||''):'group'}:${groupId}:${firstMonth(date)}`;
  }
  function monthDetail(group,list,date,teacher=false) {
    const active=list.filter(item=>!item.deleted),slots=occupiedSlots(active);
    const military=active.length>0&&active.every(item=>item.military);
    const start=active.length?active.map(item=>item.start).sort()[0]:'';
    const end=active.length?active.map(item=>item.end).sort().at(-1):'';
    const summary=military?'Военная кафедра · весь день':active.length?`${pairCount(slots.length)} · ${start}–${end}`:list.length?'Нет действующих пар · есть удалённые записи':'Нет занятий';
    const open=`<span class="month-detail-actions"><button type="button" class="text-action" data-month-calendar="${attr(group.id)}">К календарю</button><button type="button" class="text-action" data-open-month-day="${attr(date)}" data-month-group="${attr(group.id)}">Открыть день</button></span>`;
    return `<section class="month-day-detail" id="month-detail-${attr(group.id)}" data-month-group="${attr(group.id)}" data-date="${attr(date)}"><div class="month-detail-heading"><h2 class="month-selected-title">${esc(fullWeekNames[parseDate(date).getUTCDay()]+', '+formatDate(date))}</h2>${open}</div><p class="month-day-summary">${esc(summary)}</p>${teacher?teacherAgenda(list):agenda(list)}</section>`;
  }
  function monthCalendar(lessons) {
    const teacher=state.page==='teacher';
    const groups=teacher?[{id:'teacher',name:''}]:state.group==='all'?activeGroups():activeGroups().filter(group=>String(group.id)===state.group);
    return groups.map((group,index)=>{
      const filtered=group.id==='teacher'?lessons:lessons.filter(item=>String(item.group_id)===String(group.id));
      const first=firstMonth(state.date),last=lastMonth(state.date);
      const start=monday(safeDay(first)),end=shiftDay(monday(last),5);
      const dates=[];
      for(let date=start;date<=end;date=shiftDay(date,1)) if(parseDate(date).getUTCDay()!==0) dates.push(date);
      const selectionKey=monthSelectionKey(group.id);
      let selected=state.monthDays[selectionKey];
      if(!selected || selected<first || selected>last || parseDate(selected).getUTCDay()===0) {
        selected=state.date>=first&&state.date<=last&&parseDate(state.date).getUTCDay()!==0?state.date:safeDay(first);
        state.monthDays[selectionKey]=selected;
      }
      const hasMilitary=filtered.some(item=>!item.deleted&&item.military);
      const hasChanges=filtered.some(item=>item.deleted||statuses[item.status]);
      const legendTypes=[['lec','Лекция'],['prac','Практика'],['lab','Лабораторная']];
      if(filtered.some(item=>!item.deleted&&!['lec','prac','lab'].includes(item.type))) legendTypes.push(['other','Другое']);
      const legend=`<div class="month-legend" aria-label="Обозначения типов занятий">${legendTypes.map(([kind,label])=>`<span><i data-kind="${kind}" aria-hidden="true"></i>${label}</span>`).join('')}${hasChanges?'<span><i class="month-change-key" aria-hidden="true"></i>Изменения</span>':''}</div><p class="month-guide">Нажмите на дату — подробности ниже${hasMilitary?' · ВК — военная кафедра':''}</p>`;
      const calendar=`${legend}<div class="calendar" data-month-calendar-group="${attr(group.id)}" aria-label="Выберите день месяца">${weekNames.map(name=>`<span class="calendar-label">${name}</span>`).join('')}${dates.map(date=>{
        if(date<first || date>last) return '<span class="calendar-gap" aria-hidden="true"></span>';
        const all=filtered.filter(item=>item.date===date),day=all.filter(item=>!item.deleted),slots=occupiedSlots(day);
        const military=day.length>0&&day.every(item=>item.military);
        const changed=all.some(item=>item.deleted||statuses[item.status]);
        const label=fullWeekNames[parseDate(date).getUTCDay()]+', '+formatDate(date)+', '+(military?'Военная кафедра, '+pairCount(slots.length):slots.length?pairCount(slots.length):'нет занятий')+(changed?', есть изменения':'');
        const marks=slots.slice(0,7).map(slot=>`<span class="month-slot">${[...slot.kinds].map(kind=>`<i data-kind="${kind}"></i>`).join('')}</span>`).join('');
        return `<button type="button" class="calendar-cell" data-month-day="${date}" data-month-group="${attr(group.id)}" data-count="${slots.length}" data-current="${date===state.bootstrap.today}" aria-pressed="${date===selected}" aria-controls="month-detail-${attr(group.id)}" aria-label="${attr(label)}"><span class="month-cell-date"><time datetime="${date}">${Number(date.slice(-2))}</time>${changed?'<i class="month-change-dot" aria-hidden="true"></i>':''}</span><span class="month-slot-count ${military?'month-military':''}">${military?'ВК':slots.length?pairCount(slots.length):'—'}</span><span class="month-markers" aria-hidden="true">${marks}</span></button>`;
      }).join('')}</div>${monthDetail(group,filtered.filter(item=>item.date===selected),selected,teacher)}`;
      return state.group==='all' && state.page!=='teacher'?`<details class="group-block" data-block-key="${attr(group.id+':'+firstMonth(state.date))}" ${index===0?'open':''}><summary class="group-summary"><strong>${esc(group.name)}</strong>${icon('down','group-chevron')}</summary><div class="group-body">${calendar}</div></details>`:calendar;
    }).join('');
  }
  function historyEvent(event,link=false) {
    const labels={baseline:'Начало наблюдения',added:'Добавлена',removed:'Удалена',restored:'Восстановлена',changed:'Изменена'};
    const type=event.kind==='changed' && event.types?.length===1?event.types[0]:event.kind;
    const title=event.kind==='changed' && event.types?.length===1?{room:'Аудитория изменена',time:'Дата или время изменены',teacher:'Преподаватель изменён',subject:'Предмет изменён',subgroup:'Подгруппа изменена'}[event.types[0]]||'Пара изменена':labels[event.kind]||'Пара изменена';
    const before=event.before,after=event.after,item=after||before;
    const comparisons=[['room','Аудитория'],['date','Дата'],['start','Начало'],['end','Окончание'],['teacher','Преподаватель'],['subject','Предмет'],['subgroup','Подгруппа']];
    let body='';
    if(before&&after) body=comparisons.filter(([key])=>String(before[key]??'')!==String(after[key]??'')).map(([key,label])=>`<p>${label}: <span class="diff-old">${esc(before[key]||'—')}</span> → <span class="diff-new">${esc(after[key]||'—')}</span></p>`).join('');
    else body=`<p>${esc(event.kind==='baseline'?'История ведётся с этой проверки':event.kind==='removed'?'Запись исчезла из расписания':'Запись появилась в расписании')}</p>`;
    return `<div class="event" data-event="${attr(type)}" data-event-id="${attr(event.id)}">${link?`<button type="button" class="feed-link" data-history="${attr(event.lesson_id)}"><strong>${esc(title)}</strong><small>${esc(event.group_name)} · ${esc(item?.date?formatDate(item.date):'')} · ${esc(item?.subject||'Занятие')}</small></button>`:`<strong>${esc(title)}</strong>`}<time>${esc(stamp(event.detected_at))} · МСК</time>${body}</div>`;
  }
  function historyView(detail) {
    if(state.localDetail) return '<section class="history"><h2>История пары</h2><p class="settings-note">История для этой пары ещё не ведётся. Полный журнал доступен для занятий отслеживаемых групп.</p></section>';
    return `<section class="history"><div class="heading"><h2>История пары</h2><small>${detail.history.length} событий</small></div><p class="meta">Время обнаружения ботом</p><div class="feed">${detail.history.map(event=>historyEvent(event)).join('')||'<p class="meta">Для этой пары ещё нет событий</p>'}</div></section>`;
  }
  function avatar(person) {
    let url='';
    try {const candidate=new URL(person.avatar_url);if(candidate.protocol==='https:' && (candidate.hostname==='donstu.ru'||candidate.hostname.endsWith('.donstu.ru'))) url=candidate.href;} catch(_) {}
    return `<span class="avatar" aria-label="${url?'Фото преподавателя':'Инициалы преподавателя'}">${url?`<img src="${attr(url)}" alt="${attr(person.name)}" referrerpolicy="no-referrer" data-avatar-fallback="${attr(initials(person.name))}">`:esc(initials(person.name))}</span>`;
  }
  function detailsView() {
    const detail=state.detail,item=detail.lesson;
    const teacher=state.localDetail && state.teacher?.teacher ? state.teacher.teacher : {name:item.teacher};
    return `${btn('back',state.localDetail?'teacher':state.returnPage,`${icon('chevron','icon-previous')}Назад`,'back')}<div class="context"><h1>${esc(item.group_name)} · ${esc(formatDate(item.date))}</h1><p>${esc(item.deleted?'Удалённая пара':'Подробности занятия')}</p></div><div class="detail">${lessonCard(item,true)}</div><div class="detail-actions">${btn('detail-tab','details','Преподаватель',state.detailTab==='details'?'action primary':'action')}${btn('detail-tab','history','История',state.detailTab==='history'?'action primary':'action')}</div>${state.detailTab==='history'?historyView(detail):`${btn(state.localDetail?'back':'teacher-open',state.localDetail?'teacher':item.id,`${avatar(teacher)}<span class="person-text"><strong>${esc(item.teacher||'Преподаватель не указан')}</strong><small>Открыть расписание преподавателя</small></span><span class="person-arrow">${icon('chevron')}</span>`,'person')}<p class="settings-note">Двойное нажатие на пару сразу открывает её историю</p>`}`;
  }
  function settingsView() {
    const user=profile(),fields=state.bootstrap,disabled=state.busy?'disabled':'';
    const check=(key,label,description='')=>`<label class="setting"><span class="setting-label">${esc(label)}${description?`<small>${esc(description)}</small>`:''}</span><input type="checkbox" data-save="${key}" ${user[key]?'checked':''} ${disabled}></label>`;
    const disclosure=(key,title,summary,body)=>`<details class="settings-block" data-settings-section="${key}" ${state.settingsOpen[key]?'open':''}><summary><span><strong>${esc(title)}</strong><small>${esc(summary)}</small></span><span class="settings-chevron" aria-hidden="true">${icon('down')}</span></summary><div class="settings-block-body">${body}</div></details>`;
    const groupForm=(action,title,placeholder)=>`<form data-group-form="${action}" class="field-row"><label class="field"><span>${title}</span><input name="name" autocomplete="off" maxlength="40" placeholder="${placeholder}" value="${attr(state.groupDrafts[action])}" required ${disabled}></label><button type="submit" class="action" ${disabled}>${action==='primary'?'Выбрать':'Добавить'}</button></form>`;
    const primary=fields.groups.find(group=>group.primary);
    const extra=fields.groups.filter(group=>!group.primary).length;
    const groupsBody=`<div class="settings-groups">${fields.groups.map(group=>`<div class="settings-group"><span><strong>${esc(group.name)}</strong><small>${group.primary?'Основная группа':user.multigroup_enabled?'Отслеживается':'Отслеживание выключено'}</small></span>${group.primary?'':`<button type="button" class="danger" data-remove-group="${attr(group.id)}" ${disabled}>Удалить</button>`}</div>`).join('')}</div>${groupForm('primary','Основная группа','Например, ВКБ51')}<div class="settings-divider"></div>${check('multigroup_enabled','Несколько групп','Отслеживать дополнительные группы')}${user.multigroup_enabled?groupForm('add','Добавить группу','Например, ВКБ52'):''}`;
    const deliveryBody=`<label class="setting"><span class="setting-label">Время отправки<small>По Москве · кроме воскресенья</small></span><input type="time" aria-label="Время ежедневной рассылки" data-save="daily_time" value="${attr(user.daily_time)}" ${disabled}></label><label class="setting"><span class="setting-label">Формат в чате<small>Ежедневное расписание</small></span><select aria-label="Формат расписания в чате" data-save="display_format" ${disabled}>${[['image','Картинка'],['text','Текст']].map(([value,label])=>`<option value="${value}" ${user.display_format===value?'selected':''}>${label}</option>`).join('')}</select></label>`;
    const typesBody=fields.alert_types.map(item=>`<label class="setting"><span class="setting-label">${esc(item.label.replace(/^[^\p{L}\p{N}]+/u,''))}</span><input type="checkbox" data-alert="${attr(item.key)}" ${user.alert_types.includes(item.key)?'checked':''} ${disabled}></label>`).join('');
    const subjectsBody=fields.subjects.length?fields.subjects.map(item=>`<label class="setting"><span class="setting-label">${esc(item.name)}</span><input type="checkbox" data-subject="${attr(item.key)}" ${item.enabled?'checked':''} ${disabled}></label>`).join(''):'<p class="settings-note">Предметы появятся после загрузки расписания</p>';
    const muted=Object.keys(user.muted_subjects).length;
    const notificationsBody=`${check('send_schedule_on_change','Присылать новое расписание','Вместе с уведомлением об изменениях')}${disclosure('alert_types','Виды изменений',`${user.alert_types.length} из ${fields.alert_types.length} включено`,typesBody)}${disclosure('subjects','Предметы',muted?`Отключено: ${muted}`:'Все предметы включены',subjectsBody)}<p class="settings-note">Фильтры действуют со следующей проверки. Предметы остаются в расписании.</p>`;
    const general=disclosure('groups','Группы',`${primary?.name||user.group_name}${extra?` · ещё ${extra}`:''}`,groupsBody)+disclosure('delivery','Рассылка',`${user.daily_time} по Москве · ${user.display_format==='text'?'текст':'картинка'}`,deliveryBody)+disclosure('notifications','Уведомления',`${user.alert_types.length} из ${fields.alert_types.length} видов изменений${muted?` · отключено предметов: ${muted}`:''}`,notificationsBody);
    const styleChoices=[['stripe','Цветная полоса','Нейтральные карточки с цветной меткой'],['fill','Мягкая заливка','Цвет занятия заполняет карточку']].map(([value,title,description])=>`<label class="appearance-choice" data-preview-style="${value}"><span class="choice-heading"><span><strong>${title}</strong><small>${description}</small></span><input type="radio" name="miniapp_card_style" data-save="miniapp_card_style" value="${value}" ${user.miniapp_card_style===value?'checked':''} ${disabled}></span><span class="appearance-samples" aria-hidden="true">${[['lec','Лекция','Защита информации','1-282'],['prac','Практика','Иностранный язык','1-027'],['lab','Лабораторная','Программные средства','1-333А']].map(([kind,type,title,room])=>`<span class="appearance-sample" data-kind="${kind}"><span><small>${type}</small><span>${title}</span></span><strong>${room}</strong></span>`).join('')}</span></label>`).join('');
    const layouts=[['horizontal','Горизонтально'],['vertical','Вертикально'],['list','Список']].map(([value,title])=>`<label class="layout-choice"><span class="layout-diagram layout-${value}" aria-hidden="true">${Array.from({length:value==='list'?4:6},(_,i)=>`<span data-kind="${['lec','prac','lab'][i%3]}"></span>`).join('')}</span><span class="layout-choice-label"><input type="radio" name="week_layout" data-save="week_layout" value="${value}" ${user.week_layout===value?'checked':''} ${disabled}><span>${title}</span></span></label>`).join('');
    const appearance=`<section class="appearance-section"><h2>Сезонная тема</h2><div class="settings-panel">${check('seasonal_theme','Оформление времени года','Акценты в календаре и картинках бота')}</div></section><section class="appearance-section"><h2>Карточки в приложении</h2><div class="appearance-choices">${styleChoices}</div></section><section class="appearance-section"><h2>Картинка недели в боте</h2><p class="section-description">Для недельного расписания в сообщениях Telegram</p><div class="layout-choices">${layouts}</div></section>`;
    return header('Настройки')+`<div class="settings-tabs" role="tablist" aria-label="Настройки"><button type="button" id="settings-general-tab" role="tab" aria-selected="${state.settingsTab==='general'}" aria-controls="settings-general-panel" data-settings-tab="general">Основное</button><button type="button" id="settings-appearance-tab" role="tab" aria-selected="${state.settingsTab==='appearance'}" aria-controls="settings-appearance-panel" data-settings-tab="appearance">Оформление</button></div><section id="settings-general-panel" class="settings-tab-panel" role="tabpanel" aria-labelledby="settings-general-tab" ${state.settingsTab==='general'?'':'hidden'}>${general}</section><section id="settings-appearance-panel" class="settings-tab-panel" role="tabpanel" aria-labelledby="settings-appearance-tab" ${state.settingsTab==='appearance'?'':'hidden'}>${appearance}</section>`;
  }
  function settingsFocusKey(element) {
    if(!element || !view.contains(element)) return null;
    if(element.dataset.settingsTab) return `[data-settings-tab="${element.dataset.settingsTab}"]`;
    for(const key of ['save','subject','alert']) if(element.dataset[key]) return `[data-${key}="${CSS.escape(element.dataset[key])}"]${element.type==='radio'?`[value="${CSS.escape(element.value)}"]`:''}`;
    const form=element.closest('[data-group-form]');
    return form&&element.matches('input[name="name"]')?`[data-group-form="${form.dataset.groupForm}"] input[name="name"]`:null;
  }
  function visibleLesson(item) {
    return ['id','group_id','group_name','date','start','end','subject','type','teacher','room','subgroup','deleted','military','status','history_count','history_available'].map(key=>item[key]??null);
  }
  function scheduleContext() {
    const period=state.mode==='day'?state.date:state.mode==='month'?firstMonth(state.date):monday(state.date);
    return JSON.stringify([state.page,state.mode,period,state.group,state.page==='teacher'?(state.teacherChoice||state.detail?.lesson.id||''):'']);
  }
  function scheduleRevision(payload) {
    if(!payload || payload.loading) return 'loading';
    if(state.page==='teacher'&&!payload.teacher) return JSON.stringify([payload.candidates||[],payload.message]);
    if(state.mode==='day' && payload.day_revisions?.[state.date]!==undefined) return payload.day_revisions[state.date];
    if(state.mode!=='day' && payload.revision!==undefined) return payload.revision;
    return JSON.stringify((payload.lessons||[]).filter(item=>state.mode!=='day'||item.date===state.date).map(visibleLesson));
  }
  function panePosition(pane,selector) {
    const rect=pane.getBoundingClientRect();
    const first=[...pane.querySelectorAll(selector)].find(item=>item.getBoundingClientRect().bottom>rect.top+1);
    return {top:pane.scrollTop,left:pane.scrollLeft,anchor:first?(first.dataset.eventId||first.dataset.lesson):null,
      offset:first?first.getBoundingClientRect().top-rect.top:0};
  }
  function restorePanePosition(pane,saved,selector) {
    if(!saved) return;
    pane.scrollTop=saved.top;pane.scrollLeft=saved.left;
    if(saved.anchor) {
      const anchor=[...pane.querySelectorAll(selector)].find(item=>(item.dataset.eventId||item.dataset.lesson)===saved.anchor);
      if(anchor) pane.scrollTop+=anchor.getBoundingClientRect().top-pane.getBoundingClientRect().top-saved.offset;
    }
  }
  function releaseScheduleShell() {
    stopScheduleMotion();
    if(scheduleShell?.context) scrollPositions.set(scheduleShell.context,panePosition(scheduleShell.scroll,'[data-lesson]'));
    scheduleShell=null;
  }
  function setText(element,text) {if(element.textContent!==String(text)) element.textContent=String(text);}
  function compactScheduleChrome(teacher) {
    return `<div class="schedule-chrome">${teacher?`<div class="schedule-back-row">${btn('back','detail',`${icon('chevron','icon-previous')}К паре`,'schedule-back')}</div>`:''}<div class="schedule-toolbar"><div class="schedule-group-control"></div><div class="schedule-period-row"><button type="button" class="period-arrow" data-shift="-1" aria-label="Предыдущий период">${icon('chevron','icon-previous')}</button><details class="date-picker"><summary aria-label="Выбрать дату"><span class="period-title"></span>${icon('down')}</summary><label class="date-picker-popup">Перейти к дате<input type="date" aria-label="Дата расписания" data-date></label></details><button type="button" class="period-arrow" data-shift="1" aria-label="Следующий период">${icon('chevron')}</button></div><select aria-label="Период расписания" data-mode><option value="day">День</option><option value="week">Неделя</option><option value="month">Месяц</option></select></div>${dateStrip()}<div class="freshness"><span class="freshness-text"></span><span class="freshness-actions">${btn('today','1','Сегодня','freshness-today')}${teacher?'':`<button type="button" data-refresh="1" class="refresh-button" aria-label="Обновить расписание" title="Обновить">${icon('refresh')}</button>`}</span></div></div>`;
  }
  function ensureScheduleShell() {
    if(scheduleShell?.page===state.page && view.contains(scheduleShell.chrome)) return scheduleShell;
    releaseScheduleShell();feedShell=null;
    view.innerHTML=compactScheduleChrome(state.page==='teacher')+`<div class="schedule-scroll">${scheduleViewport('')}</div>`;
    view.scrollTop=0;
    scheduleShell={page:state.page,chrome:view.querySelector('.schedule-chrome'),scroll:view.querySelector('.schedule-scroll'),
      viewport:view.querySelector('.schedule-viewport'),content:view.querySelector('.schedule-content'),context:null,key:null,roster:null};
    return scheduleShell;
  }
  function patchScheduleChrome(shell,options={}) {
    const chrome=shell.chrome,teacher=state.page==='teacher';
    const groups=activeGroups();
    const person=state.teacher?.teacher;
    const roster=teacher?JSON.stringify([person?.id,person?.name,person?.avatar_url,state.detail?.lesson.teacher]):JSON.stringify(groups.map(group=>[group.id,group.name]));
    if(shell.roster!==roster) {
      const slot=chrome.querySelector('.schedule-group-control');
      slot.innerHTML=teacher?`<div class="schedule-person">${avatar(person||{name:state.detail?.lesson.teacher})}<strong>${esc(person?.name||state.detail?.lesson.teacher||'Преподаватель')}</strong></div>`:groups.length>1?`<select aria-label="Группа расписания" data-group>${groups.map(group=>`<option value="${attr(group.id)}">${esc(group.name)}</option>`).join('')}<option value="all">Все группы</option></select>`:`<span class="group-label" role="heading" aria-level="1">${esc(groups[0]?.name||profile().group_name||'')}</span>`;
      shell.roster=roster;
    }
    const groupControl=chrome.querySelector('[data-group]');
    if(groupControl && groupControl.value!==state.group) groupControl.value=state.group;
    const modeControl=chrome.querySelector('[data-mode]');
    if(modeControl.value!==state.mode) modeControl.value=state.mode;
    let period=formatDate(state.date,{month:'long',year:'numeric'}).replace(/ г\.$/,'');
    if(state.mode==='week') {
      const start=monday(state.date),end=shiftDay(start,5);
      period=start.slice(0,7)===end.slice(0,7)?`${Number(start.slice(-2))}–${formatDate(end,{day:'numeric',month:'short'})}`:`${formatDate(start,{day:'numeric',month:'short'})} – ${formatDate(end,{day:'numeric',month:'short'})}`;
    }
    setText(chrome.querySelector('.period-title'),period[0].toLocaleUpperCase('ru-RU')+period.slice(1));
    const picker=chrome.querySelector('.date-picker'),input=picker.querySelector('[data-date]');
    const inputType=state.mode==='month'?'month':'date';
    if(input.type!==inputType) input.type=inputType;
    if(options.navigation || (!picker.open&&document.activeElement!==input)) {
      const value=state.mode==='month'?state.date.slice(0,7):state.date;
      if(input.value!==value) input.value=value;
    }
    chrome.querySelector('.schedule-period-row').classList.toggle('is-day-period',state.mode==='day');
    chrome.dataset.viewMode=state.mode;
    chrome.querySelectorAll('.period-arrow').forEach(button=>button.hidden=state.mode==='day');
    const dayControls=chrome.querySelector('.day-controls');dayControls.hidden=state.mode!=='day';
    const start=monday(state.date);
    chrome.querySelectorAll('[data-day]').forEach((button,index)=>{
      const date=shiftDay(start,index);
      button.dataset.day=date;button.setAttribute('aria-pressed',String(date===state.date));button.setAttribute('aria-label',formatDate(date));
      setText(button.querySelector('small'),weekNames[index]);setText(button.querySelector('span'),Number(date.slice(-2)));
    });
    const payload=teacher?state.teacher:state.schedule;
    const fallback=groups.filter(group=>state.group==='all'||String(group.id)===state.group);
    const values=teacher?[payload?.freshness||{has_snapshot:false}]:(payload?.freshness||fallback);
    const failed=values.some(item=>item?.last_fetch_failed),missing=values.some(item=>!item?.has_snapshot);
    const times=values.map(item=>item?.last_fetch_at).filter(Boolean).sort();
    const label=missing?'Расписание ещё не загружено':failed?'Нет связи · сохранено '+stamp(times[0]):'Проверено '+stamp(times[0]);
    const freshness=chrome.querySelector('.freshness');freshness.classList.toggle('stale',failed||missing);
    setText(freshness.querySelector('.freshness-text'),label);
  }
  function renderSchedule(options={}) {
    const shell=ensureScheduleShell();patchScheduleChrome(shell,options);
    const payload=state.page==='teacher'?state.teacher:state.schedule;
    const context=scheduleContext();
    const monthChoices=state.mode==='month'?Object.entries(state.monthDays).filter(([key])=>key.endsWith(firstMonth(state.date))):null;
    const key=JSON.stringify([context,scheduleRevision(payload),payload?.slots||[],monthChoices,state.error?.message||null]);
    if(shell.key===key) return;
    const sameContext=shell.context===context;
    const saved=sameContext?panePosition(shell.scroll,'[data-lesson]'):scrollPositions.get(context);
    if(shell.context&&!sameContext) scrollPositions.set(shell.context,panePosition(shell.scroll,'[data-lesson]'));
    const opened=new Map([...shell.content.querySelectorAll('[data-block-key]')].map(section=>[section.dataset.blockKey,section.open]));
    let body;
    if(state.error) body=`<div class="notice" role="alert"><h2>Не удалось загрузить</h2><p>${esc(state.error.message)}</p>${btn('retry','1','Попробовать ещё раз')}</div>`;
    else if(payload?.candidates?.length&&!payload.teacher) body='<p class="meta">Найдено несколько преподавателей. Выберите нужного.</p>'+payload.candidates.map(person=>btn('teacher-choice',person.id,`${avatar(person)}<span class="person-text"><strong>${esc(person.name)}</strong></span><span class="person-arrow">${icon('chevron')}</span>`,'person')).join('');
    else if(state.page==='teacher'&&payload&&!payload.teacher) body=`<div class="empty">${esc(payload.message||'Преподаватель не найден в ДГТУ')}</div>`;
    else body=!payload||payload.loading?'<p class="busy">Загрузка расписания…</p>':scheduleBody(payload.lessons,state.page==='teacher');
    shell.content.innerHTML=body;
    shell.content.querySelectorAll('[data-block-key]').forEach(section=>{if(opened.has(section.dataset.blockKey)) section.open=opened.get(section.dataset.blockKey);});
    shell.viewport.toggleAttribute('data-swipe-days',state.mode==='day');
    if(options.navigation) {shell.scroll.scrollTop=0;shell.scroll.scrollLeft=0;}
    else restorePanePosition(shell.scroll,saved,'[data-lesson]');
    shell.context=context;
    // monthCalendar initializes the selected date map on its first render.
    const choices=state.mode==='month'?Object.entries(state.monthDays).filter(([name])=>name.endsWith(firstMonth(state.date))):null;
    shell.key=JSON.stringify([context,scheduleRevision(payload),payload?.slots||[],choices,state.error?.message||null]);
    if(options.direction&&payload&&!payload.loading&&!state.error) animateSchedule(options.direction);
  }
  function eventRevision(event) {
    return [event.id,event.lesson_id,event.kind,event.detected_at,event.group_id,event.group_name,event.types,
      event.before?visibleLesson(event.before):null,event.after?visibleLesson(event.after):null];
  }
  function renderChanges() {
    if(!feedShell || !view.contains(feedShell.chrome)) {
      releaseScheduleShell();view.innerHTML=`<div class="changes-chrome">${header('Изменения')}<p class="meta changes-count"></p></div><div class="changes-scroll"><div class="feed-content"></div></div>`;
      view.scrollTop=0;feedShell={chrome:view.querySelector('.changes-chrome'),scroll:view.querySelector('.changes-scroll'),content:view.querySelector('.feed-content'),key:null};
    }
    setText(feedShell.chrome.querySelector('.changes-count'),`Хронология${state.totalEvents!==null?' · '+state.totalEvents+' изменений':''} · время обнаружения ботом`);
    const key=JSON.stringify([state.events.map(eventRevision),state.nextCursor,state.nextOffset,state.error?.message]);
    if(key===feedShell.key) return;
    const saved=panePosition(feedShell.scroll,'[data-event-id]');
    const more=state.nextCursor!==null||state.nextOffset!==null;
    feedShell.content.innerHTML=state.error?`<div class="notice" role="alert"><p>${esc(state.error.message)}</p>${btn('retry','1','Попробовать ещё раз')}</div>`:`<div class="feed">${state.events.length?state.events.map(event=>historyEvent(event,true)).join(''):'<div class="empty">Изменений пока нет<p>Журнал заполняется при проверках расписания</p></div>'}</div>`+(more?btn('more-events',state.nextOffset??0,'Показать ещё','action block-button'):'');
    restorePanePosition(feedShell.scroll,saved,'[data-event-id]');feedShell.key=key;
  }
  function render(options={}) {
    if(!['schedule','teacher'].includes(state.page)) stopScheduleMotion();
    app.dataset.season=season();
    app.dataset.cardStyle=profile().miniapp_card_style||'stripe';
    navigation.hidden=!state.bootstrap;
    navigation.querySelectorAll('[data-page]').forEach(button=>{
      const active=button.dataset.page===(['detail','teacher'].includes(state.page)?state.returnPage:state.page);
      if(active) button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
    });
    try {if(['detail','teacher'].includes(state.page)) telegram?.BackButton?.show();else telegram?.BackButton?.hide();} catch(_) {}
    const fixed=Boolean(state.bootstrap&&['schedule','teacher','changes'].includes(state.page));
    app.classList.toggle('is-fixed-view',fixed);
    if(fixed&&state.page!=='changes') {renderSchedule(options);return;}
    if(fixed&&state.page==='changes') {renderChanges();return;}
    releaseScheduleShell();feedShell=null;
    if(state.error) {
      view.innerHTML=header('Расписание')+`<div class="notice" role="alert"><h2>Не удалось загрузить</h2><p>${esc(state.error.message)}</p>${btn('retry','1','Попробовать ещё раз')}</div>`;return;
    }
    if(state.page==='settings') {
      const focus=settingsFocusKey(document.activeElement)||state.settingsFocus;
      view.innerHTML=settingsView();
      const replacement=focus?view.querySelector(focus):null;
      if(replacement&&!replacement.disabled&&replacement.getClientRects().length) replacement.focus({preventScroll:true});
      if(!state.busy) state.settingsFocus=null;
      return;
    }
    if(state.page==='detail' && state.detail) {view.innerHTML=detailsView();return;}
    view.innerHTML=header(state.page==='detail'?'Занятие':state.page==='teacher'?'Преподаватель':state.page==='changes'?'Изменения':'Расписание')+'<p class="busy">Загрузка…</p>';
  }
  function applyBootstrap(response) {
    const roster=JSON.stringify(response.groups.map(group=>[group.id,group.name,group.primary,group.enabled]));
    if(rosterVersion && rosterVersion!==roster) {
      clearScheduleCache();state.events=[];state.nextOffset=null;state.nextCursor=null;state.totalEvents=null;state.feedRevision=null;state.feedLoadedOlder=false;
    }
    rosterVersion=roster;
    state.bootstrap=response;
    response.profile.display_format ??= 'image';
    response.profile.alert_types ??= response.alert_types.map(item=>item.key);
    response.profile.muted_subjects ??= {};
    response.profile.miniapp_card_style ??= 'stripe';
    if(!state.date) state.date=safeDay(response.today);
    if(!activeGroups().some(group=>String(group.id)===state.group) && state.group!=='all') state.group=String(response.profile.group_id);
    if(state.group==='all' && activeGroups().length<2) state.group=String(response.profile.group_id);
  }
  async function bootstrap() {applyBootstrap(await api('/api/bootstrap'));}
  async function loadPage(options={}) {
    clearTimeout(tapTimer);tapTimer=null;lastTap=null;
    stopScheduleMotion(Boolean(options.direction));
    const version=++requestVersion;state.error=null;setBusy(true);
    let cached=null, page=state.page;
    try {
      if(!state.bootstrap) await bootstrap();
      if(version!==requestVersion) return;
      const selectedRange=range();
      if(page==='schedule' || page==='teacher') {
        const url=page==='schedule'?'/api/schedule?'+new URLSearchParams({group_id:state.group,...selectedRange}):'/api/teacher?'+new URLSearchParams({lesson_id:state.detail.lesson.id,...selectedRange,...(state.teacherChoice?{teacher_id:state.teacherChoice}:{})});
        cached=scheduleCache.get(url);
        if(cached) state[page]=cached.value;
        else if(options.keep && scheduleShell?.context===scheduleContext() && state[page]) { /* Keep the visible snapshot during a background check. */ }
        else if(page==='schedule') state.schedule=null;
        else if(state.teacher?.teacher) state.teacher={...state.teacher,lessons:[],loading:true};
        else state.teacher=null;
        if(cached && !options.force && Date.now()-cached.at<60000) return;
        render(options);
        const epoch=cacheEpoch;
        const result=await api(url);
        if(epoch===cacheEpoch && version===requestVersion) {
          scheduleCache.delete(url);scheduleCache.set(url,{value:result,at:Date.now()});
          while(scheduleCache.size>8) scheduleCache.delete(scheduleCache.keys().next().value);
        }
        if(version!==requestVersion) return;state[page]=result;
      } else if(page==='detail') {
        render();
        const result=await api('/api/lessons/'+encodeURIComponent(options.lessonId||state.detail?.lesson.id||state.lessonId));
        if(version!==requestVersion) return;state.detail=result;
      } else if(page==='changes') {
        render(options);
        const query={limit:50};
        if(options.append && state.nextCursor!==null) query.before_id=state.nextCursor;
        else query.offset=options.offset||0;
        const result=await api('/api/changes?'+new URLSearchParams(query));
        if(version!==requestVersion) return;
        const hadOlder=state.feedLoadedOlder;
        const previousIds=new Set(state.events.map(event=>String(event.id)));
        const gap=options.keep&&!options.append&&hadOlder&&result.events.length>0&&
          (result.next_cursor!=null||result.next_offset!=null)&&!result.events.some(event=>previousIds.has(String(event.id)));
        // Event IDs keep older pages stable when new changes arrive at the top.
        const merged=new Map((options.append||options.keep?state.events:[]).map(event=>[String(event.id),event]));
        for(const event of result.events) merged.set(String(event.id),event);
        state.events=[...merged.values()].sort((a,b)=>Number(b.id)-Number(a.id));
        if(!options.keep || options.append || !hadOlder || gap) {
          state.nextOffset=result.next_offset??null;state.nextCursor=result.next_cursor??null;
        }
        state.feedLoadedOlder=options.append||Boolean(options.keep&&hadOlder);
        state.totalEvents=result.total_events??null;state.feedRevision=result.revision??null;
      } else if(page==='settings') {
        await bootstrap();
      }
    } catch(error) {
      if(version===requestVersion && !options.silent) {
        if(cached && ![401,403].includes(error.status)) notify('Нет связи с сервером · показана сохранённая версия');
        else state.error=error;
      }
    }
    finally {if(version===requestVersion) {setBusy(false);render(options);}}
  }
  async function queuedAction(path,body) {
    if(state.busy) return false;
    setBusy(true);notify('Сохраняю…',0);
    try {
      const job=await api(path,body);
      let result=job;
      const until=Date.now()+45000;
      while(!['done','failed'].includes(result.status) && Date.now()<until) {
        await new Promise(resolve=>setTimeout(resolve,500));
        result=await api('/api/jobs/'+encodeURIComponent(job.job_id||job.id));
      }
      if(result.status==='failed') throw new Error(typeof result.error==='string'?result.error:result.error?.message||'Не удалось сохранить настройку');
      if(result.status!=='done') {notify('Запрос ещё выполняется. Настройка появится после обработки ботом.',7000);return false;}
      clearScheduleCache();await bootstrap();notify(path==='/api/refresh'?'Расписание проверено':'Сохранено');
      return true;
    } catch(error) {notify(error.message,7000);return false;}
    finally {setBusy(false);}
  }
  async function openLesson(id,history=false) {
    clearTimeout(tapTimer);tapTimer=null;lastTap=null;
    state.lessonId=id;
    const teacherItem=state.page==='teacher'?state.teacher?.lessons.find(item=>item.id===id):null;
    if(teacherItem?.history_available===false) {
      state.sourceDetail=state.detail;state.detail={lesson:teacherItem,history:[]};
      state.localDetail=true;state.detailTab=history?'history':'details';state.page='detail';state.error=null;render();return;
    }
    if(!['detail','teacher'].includes(state.page)) state.returnPage=state.page;
    state.page='detail';state.detail=null;state.detailTab=history?'history':'details';state.localDetail=false;
    await loadPage({lessonId:id});
  }
  function activateLesson(id,event) {
    if(event.detail===0) {openLesson(id,false);return;}
    const now=Date.now();
    if(lastTap?.id===id && now-lastTap.time<360) {clearTimeout(tapTimer);tapTimer=null;lastTap=null;openLesson(id,true);return;}
    clearTimeout(tapTimer);lastTap={id,time:now};
    tapTimer=setTimeout(()=>{lastTap=null;tapTimer=null;openLesson(id,false);},360);
  }
  function navigateBack() {
    clearTimeout(tapTimer);lastTap=null;
    if(state.page==='detail' && state.localDetail) {state.detail=state.sourceDetail;state.localDetail=false;state.page='teacher';state.error=null;render();return;}
    if(state.page==='teacher') {state.page='detail';state.error=null;render();}
    else if(state.page==='detail') {state.page=state.returnPage;loadPage();}
  }
  const reduceMotion=()=>window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const scheduleEase='cubic-bezier(.22,1,.36,1)';
  let scheduleTransition=null;
  function stopScheduleMotion(keepExit=false) {
    if(pageMotion) {pageMotion.cancel();pageMotion=null;}
    if(scheduleTransition&&(!keepExit||scheduleTransition.target!==scheduleContext())) {
      const previous=scheduleTransition;scheduleTransition=null;
      clearTimeout(previous.timer);previous.exit.cancel();previous.frame.remove();
    }
  }
  function captureScheduleMotion(direction,content=scheduleShell?.content) {
    stopScheduleMotion();
    const pane=scheduleShell?.scroll;
    if(!content||!pane||reduceMotion()||!content.animate||state.mode!=='day') return null;
    const box=pane.getBoundingClientRect(),source=content.getBoundingClientRect();
    if(box.width<=0||box.height<=0) return null;
    const offset=source.left-box.left;
    const frame=document.createElement('div');
    frame.className='schedule-motion-frame';frame.setAttribute('aria-hidden','true');frame.inert=true;
    Object.assign(frame.style,{position:'fixed',top:box.top+'px',left:box.left+'px',width:box.width+'px',height:box.height+'px',overflow:'hidden',pointerEvents:'none',zIndex:'3'});
    const copy=content.cloneNode(true);
    copy.classList.remove('is-dragging');
    copy.removeAttribute('id');
    copy.querySelectorAll('[id]').forEach(item=>item.removeAttribute('id'));
    copy.querySelectorAll('[data-lesson],[data-history]').forEach(item=>{item.removeAttribute('data-lesson');item.removeAttribute('data-history');});
    Object.assign(copy.style,{position:'absolute',top:(source.top-box.top)+'px',left:'0',width:content.offsetWidth+'px',minHeight:'0',margin:'0',transform:`translate3d(${offset}px,0,0)`,willChange:'transform,opacity'});
    frame.append(copy);pane.append(frame);
    const exit=copy.animate([{transform:`translate3d(${offset}px,0,0)`,opacity:1},{transform:`translate3d(${-Math.sign(direction)*box.width}px,0,0)`,opacity:.88}],{duration:220,easing:scheduleEase,fill:'forwards'});
    const transition={frame,exit,width:box.width,offset,direction:Math.sign(direction),target:null,timer:null};
    const clear=()=>{frame.remove();clearTimeout(transition.timer);if(scheduleTransition===transition)scheduleTransition=null;};
    exit.finished.then(clear,clear);
    transition.timer=setTimeout(clear,280);
    scheduleTransition=transition;
    return transition;
  }
  function animateSchedule(direction) {
    const content=view.querySelector('.schedule-content');
    if(!direction || !content || state.mode!=='day' || reduceMotion() || !content.animate) return;
    if(pageMotion) pageMotion.cancel();
    const transition=scheduleTransition?.target===scheduleContext()?scheduleTransition:null;
    const distance=transition?transition.direction*transition.width+transition.offset:Math.sign(direction)*28;
    const incoming=content.animate([
      {transform:`translate3d(${distance}px,0,0)`,opacity:transition?1:.65},
      {transform:'translate3d(0,0,0)',opacity:1}
    ],{duration:220,easing:scheduleEase});
    if(transition&&transition.exit.currentTime!==null) incoming.currentTime=transition.exit.currentTime;
    pageMotion=incoming;
    const clear=()=>{if(pageMotion===incoming)pageMotion=null;};
    incoming.finished.then(clear,clear);
  }
  async function shiftSchedule(amount,transition=null) {
    if(state.mode==='day'&&!transition) transition=captureScheduleMotion(amount);
    if(state.mode==='month') state.date=shiftMonth(state.date,amount);
    else if(state.mode==='week') state.date=shiftDay(state.date,7*amount);
    else {
      state.date=shiftDay(state.date,amount);
      if(parseDate(state.date).getUTCDay()===0) state.date=shiftDay(state.date,amount);
    }
    if(transition) transition.target=scheduleContext();
    await loadPage({direction:amount,navigation:true});
  }
  function cancelGesture(bounce=true) {
    const current=gesture;gesture=null;
    if(!current) return;
    const transform=current.content.style.transform;
    current.content.style.transform='';current.content.classList.remove('is-dragging');
    try {if(current.viewport.hasPointerCapture(current.id)) current.viewport.releasePointerCapture(current.id);} catch(_) {}
    if(bounce && current.axis==='x' && !reduceMotion() && current.content.animate) {
      current.content.animate([{transform},{transform:'translate3d(0,0,0)'}],{duration:180,easing:scheduleEase});
    }
  }
  app.addEventListener('pointerdown',event=>{
    // A fresh contact is an intentional tap, not the trailing click from a swipe.
    suppressClickUntil=0;
    if(gesture) cancelGesture();
    if(event.isPrimary===false || event.pointerType==='mouse' || event.button>0 || state.busy || state.mode!=='day' || !['schedule','teacher'].includes(state.page)) return;
    const viewport=event.target.closest('[data-swipe-days]');
    if(!viewport || event.target.closest('a,input,select,textarea,[contenteditable],button:not([data-lesson])')) return;
    const content=viewport.querySelector('.schedule-content');
    if(!content) return;
    gesture={id:event.pointerId,x:event.clientX,y:event.clientY,time:performance.now(),viewport,content,axis:null};
  });
  app.addEventListener('pointermove',event=>{
    const current=gesture;
    if(!current || current.id!==event.pointerId) return;
    const dx=event.clientX-current.x,dy=event.clientY-current.y;
    if(!current.axis) {
      if(Math.abs(dy)>8 && Math.abs(dy)>Math.abs(dx)*1.15) {cancelGesture(false);return;}
      if(Math.abs(dx)<10 || Math.abs(dx)<Math.abs(dy)*1.4) return;
      current.axis='x';clearTimeout(tapTimer);tapTimer=null;lastTap=null;
      if(pageMotion) {pageMotion.cancel();pageMotion=null;}
      try {current.viewport.setPointerCapture(current.id);} catch(_) {}
      current.content.classList.add('is-dragging');
    }
    if(event.cancelable) event.preventDefault();
    if(!reduceMotion()) {
      const distance=Math.min(120,current.viewport.clientWidth*.4);
      current.content.style.transform=`translate3d(${Math.max(-distance,Math.min(distance,dx))}px,0,0)`;
    }
  },{passive:false});
  app.addEventListener('pointerup',event=>{
    const current=gesture;
    if(!current || current.id!==event.pointerId) return;
    const dx=event.clientX-current.x,dy=event.clientY-current.y;
    const elapsed=Math.max(1,performance.now()-current.time);
    const horizontal=current.axis==='x';
    const accepted=horizontal && Math.abs(dx)>Math.abs(dy)*1.4 && (Math.abs(dx)>=48 || (Math.abs(dx)>=24 && Math.abs(dx)/elapsed>=.45));
    if(horizontal) {
      suppressClickUntil=Date.now()+500;
      clearTimeout(tapTimer);tapTimer=null;lastTap=null;
    }
    const direction=dx<0?1:-1;
    const transition=accepted?captureScheduleMotion(direction,current.content):null;
    cancelGesture(!accepted);
    if(accepted) shiftSchedule(direction,transition);
  });
  app.addEventListener('pointercancel',()=>cancelGesture());
  app.addEventListener('lostpointercapture',event=>{
    // Touch starts with implicit capture on the card. Moving capture to the
    // viewport also emits this event on that old target; the swipe is still active.
    if(gesture?.id===event.pointerId && event.target===gesture.viewport) cancelGesture();
  });
  app.addEventListener('click',event=>{
    if(event.detail!==0 && Date.now()<suppressClickUntil) {event.preventDefault();event.stopImmediatePropagation();}
  },true);
  async function syncLive() {
    if(document.visibilityState==='hidden' || syncing || gesture || pageMotion?.playState==='running' || state.busy || tapTimer || !state.bootstrap || !['schedule','teacher','changes'].includes(state.page)) return;
    syncing=true;
    const signature=JSON.stringify([state.page,state.date,state.group,state.mode,requestVersion]);
    try {
      const result=await api('/api/bootstrap');
      if(gesture || state.busy || tapTimer || signature!==JSON.stringify([state.page,state.date,state.group,state.mode,requestVersion]) || document.visibilityState==='hidden') return;
      applyBootstrap(result);
      await loadPage({keep:true,silent:true,force:true});
    } catch(_) { /* Keep the last visible snapshot when the web connection fails. */ }
    finally {syncing=false;}
  }
  app.addEventListener('click',async event=>{
    const target=event.target.closest('button');
    if(!target || target.disabled) return;
    if(target.dataset.lesson) {activateLesson(target.dataset.lesson,event);return;}
    if(target.dataset.history) {openLesson(target.dataset.history,true);return;}
    clearTimeout(tapTimer);tapTimer=null;lastTap=null;
    if(target.dataset.settingsTab) {
      state.settingsTab=target.dataset.settingsTab;render();
      view.querySelector(`[data-settings-tab="${state.settingsTab}"]`)?.focus({preventScroll:true});
    }
    else if(target.dataset.page) {state.page=target.dataset.page;await loadPage();}
    else if(target.dataset.back) {if(target.dataset.back==='teacher' && state.localDetail) {state.detail=state.sourceDetail;state.localDetail=false;state.page='teacher';state.error=null;render();}else if(target.dataset.back==='detail') {state.page='detail';state.error=null;render();}else {state.page=target.dataset.back;await loadPage();}}
    else if(target.dataset.detailTab) {state.detailTab=target.dataset.detailTab;render();}
    else if(target.dataset.teacherOpen) {state.page='teacher';state.teacherChoice='';state.date=state.detail.lesson.date;await loadPage({navigation:true});}
    else if(target.dataset.teacherChoice) {state.teacherChoice=target.dataset.teacherChoice;await loadPage({navigation:true});}
    else if(target.dataset.day) {const direction=target.dataset.day>state.date?1:-1;state.date=target.dataset.day;await loadPage({direction,navigation:true});}
    else if(target.dataset.monthDay) {
      const date=target.dataset.monthDay,group=target.dataset.monthGroup;
      state.monthDays[monthSelectionKey(group,date)]=date;state.date=date;render();
      const cell=view.querySelector(`[data-month-day="${CSS.escape(date)}"][data-month-group="${CSS.escape(group)}"]`);
      cell?.focus({preventScroll:true});
      const detail=view.querySelector(`.month-day-detail[data-month-group="${CSS.escape(group)}"]`);
      if(detail&&scheduleShell) scheduleShell.scroll.scrollTo({top:detail.getBoundingClientRect().top-scheduleShell.scroll.getBoundingClientRect().top+scheduleShell.scroll.scrollTop,behavior:reduceMotion()?'instant':'smooth'});
    }
    else if(target.dataset.monthCalendar) {
      const calendar=view.querySelector(`[data-month-calendar-group="${CSS.escape(target.dataset.monthCalendar)}"]`);
      if(calendar&&scheduleShell) scheduleShell.scroll.scrollTo({top:calendar.getBoundingClientRect().top-scheduleShell.scroll.getBoundingClientRect().top+scheduleShell.scroll.scrollTop-58,behavior:reduceMotion()?'instant':'smooth'});
    }
    else if(target.dataset.openMonthDay) {
      state.date=target.dataset.openMonthDay;
      if(state.page!=='teacher'&&target.dataset.monthGroup) state.group=target.dataset.monthGroup;
      state.mode='day';await loadPage({navigation:true});
    }
    else if(target.dataset.shift) {
      await shiftSchedule(Number(target.dataset.shift));
    } else if(target.dataset.today) {state.date=safeDay(state.bootstrap.today);state.monthDays={};await loadPage({navigation:true});}
    else if(target.dataset.retry) await loadPage();
    else if(target.dataset.moreEvents) await loadPage({offset:target.dataset.moreEvents,append:true,keep:true});
    else if(target.dataset.refresh) {if(await queuedAction('/api/refresh',{group_id:state.group})) await loadPage({keep:true,force:true});}
    else if(target.dataset.removeGroup) {
      if(confirm('Удалить эту дополнительную группу из отслеживания?') && await queuedAction('/api/groups',{action:'remove',group_id:Number(target.dataset.removeGroup)})) render();
    }
  });
  app.addEventListener('dblclick',event=>{if(event.target.closest('[data-lesson]')) event.preventDefault();});
  app.addEventListener('keydown',event=>{
    if(!event.target.matches('[data-settings-tab]') || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    state.settingsTab=event.key==='Home'?'general':event.key==='End'?'appearance':state.settingsTab==='general'?'appearance':'general';
    render();view.querySelector(`[data-settings-tab="${state.settingsTab}"]`)?.focus({preventScroll:true});
  });
  app.addEventListener('toggle',event=>{
    const key=event.target.dataset.settingsSection;
    if(key&&view.contains(event.target)) state.settingsOpen[key]=event.target.open;
  },true);
  app.addEventListener('input',event=>{
    const form=event.target.closest('[data-group-form]');
    if(form&&event.target.matches('input[name="name"]')) state.groupDrafts[form.dataset.groupForm]=event.target.value;
  });
  app.addEventListener('change',async event=>{
    const target=event.target;
    if(target.hasAttribute('data-group')) {state.group=target.value;await loadPage({navigation:true});}
    else if(target.hasAttribute('data-mode')) {state.mode=target.value;await loadPage({navigation:true});}
    else if(target.hasAttribute('data-date')) {
      const value=target.value.length===7?target.value+'-01':target.value;
      if(/^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(parseDate(value).valueOf())) {state.date=safeDay(value);target.closest('details')?.removeAttribute('open');await loadPage({navigation:true});}
    } else if(target.dataset.save) {
      if(target.type==='radio'&&!target.checked) return;
      state.settingsFocus=settingsFocusKey(target);
      const value=target.type==='checkbox'?target.checked:target.value;
      const success=await queuedAction('/api/settings',{[target.dataset.save]:value});
      if(!success) await bootstrap().catch(()=>{});
      render();
    } else if(target.dataset.alert) {
      state.settingsFocus=settingsFocusKey(target);
      const selected=new Set(profile().alert_types);
      if(target.checked) selected.add(target.dataset.alert);else selected.delete(target.dataset.alert);
      await queuedAction('/api/settings',{alert_types:[...selected]});render();
    } else if(target.dataset.subject) {
      state.settingsFocus=settingsFocusKey(target);
      const muted={...profile().muted_subjects};
      const subject=state.bootstrap.subjects.find(item=>item.key===target.dataset.subject);
      if(target.checked) delete muted[target.dataset.subject];else muted[target.dataset.subject]=subject.name;
      await queuedAction('/api/settings',{muted_subjects:muted});render();
    }
  });
  app.addEventListener('submit',async event=>{
    const form=event.target.closest('[data-group-form]');
    if(!form) return;
    event.preventDefault();
    const name=new FormData(form).get('name').trim();
    if(!name) return;
    state.settingsFocus=`[data-group-form="${form.dataset.groupForm}"] input[name="name"]`;
    if(await queuedAction('/api/groups',{action:form.dataset.groupForm,name})) {state.groupDrafts[form.dataset.groupForm]='';form.reset();render();}
  });
  app.addEventListener('error',event=>{
    const image=event.target;
    if(image instanceof HTMLImageElement && image.parentElement && image.dataset.avatarFallback!==undefined) {
      image.parentElement.textContent=image.dataset.avatarFallback;
    }
  },true);
  if(!initData) {
    view.setAttribute('aria-busy','false');
    view.innerHTML='<div class="notice"><h1>Расписание ДГТУ</h1><p>Откройте приложение кнопкой в Telegram-боте. Так загрузятся ваши группы и личные настройки.</p></div>';
    return;
  }
  syncTheme();
  telegram.ready();telegram.expand();
  function updateViewport(event) {
    if(event?.isStateStable===false) return;
    const height=Number(telegram.viewportStableHeight||window.innerHeight);
    if(height>0) app.style.setProperty('--app-height',height+'px');
  }
  updateViewport();
  window.addEventListener('resize',()=>updateViewport());
  telegram.onEvent?.('viewportChanged',updateViewport);
  telegram.onEvent?.('themeChanged',syncTheme);
  telegram.BackButton?.onClick(navigateBack);
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible') syncLive();});
  try {if(!telegram.isVersionAtLeast || telegram.isVersionAtLeast('8.0')) telegram.onEvent?.('activated',syncLive);} catch(_) {}
  setInterval(syncLive,60000);
  loadPage();
})();
