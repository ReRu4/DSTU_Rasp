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
  const seasons = {autumn:'Осень',winter:'Зима',spring:'Весна',summer:'Лето',classic:'Без сезона'};
  const state = {
    page:'schedule', mode:'day', date:'', group:'', bootstrap:null, schedule:null,
    detail:null, teacher:null, teacherChoice:'', events:[], nextOffset:null,
    returnPage:'schedule', lessonId:'', detailTab:'details', sourceDetail:null, localDetail:false, busy:false, error:null,
    settingsTab:'general', settingsOpen:{delivery:true}, groupDrafts:{primary:'',add:''}, settingsFocus:null
  };
  let requestVersion=0, tapTimer=null, lastTap=null, statusTimer=null, syncing=false;
  const scheduleCache=new Map();
  let rosterVersion='', cacheEpoch=0, gesture=null, suppressClickUntil=0, pageMotion=null;
  function clearScheduleCache() {scheduleCache.clear();cacheEpoch++;}
  const esc = value => String(value??'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const attr = esc;
  const btn=(action,value,text,cls='action')=>`<button type="button" class="${cls}" data-${action}="${attr(value)}">${text}</button>`;
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
  function header(title,withToday=false) {
    const heading=withToday && state.mode==='day'?`<details class="date-picker"><summary aria-label="Выбрать дату"><h1>${esc(title)}</h1><span aria-hidden="true">⌄</span></summary><label class="date-picker-popup">Перейти к дате<input type="date" aria-label="Дата расписания" data-date value="${attr(state.date)}"></label></details>`:`<h1>${esc(title)}</h1>`;
    return `<header class="header"><div class="header-line"><span>ДГТУ · Расписание</span><span class="season-label">${seasons[season()]}</span></div><div class="title-line">${heading}${withToday?btn('today','1','Сегодня','quiet-button'):''}</div></header>`;
  }
  function toolbar(teacher=false) {
    const groups=activeGroups();
    const selector=teacher?`<input type="${state.mode==='month'?'month':'date'}" aria-label="Дата расписания" data-date value="${attr(state.mode==='month'?state.date.slice(0,7):state.date)}">`:groups.length>1?`<select aria-label="Группа расписания" data-group>${groups.map(group=>`<option value="${attr(group.id)}" ${String(group.id)===state.group?'selected':''}>${esc(group.name)}</option>`).join('')}<option value="all" ${state.group==='all'?'selected':''}>Все группы</option></select>`:`<span class="group-label">${esc(groups[0]?.name||profile().group_name||'')}</span>`;
    const dates=state.mode==='day'?dateStrip():`<div class="date-nav">${btn('shift','-1','‹','') }<input type="${state.mode==='month'?'month':'date'}" aria-label="Дата расписания" data-date value="${attr(state.mode==='month'?state.date.slice(0,7):state.date)}">${btn('shift','1','›','')}</div>`;
    return `<div class="toolbar">${selector}<select aria-label="Период расписания" data-mode>${[['day','День'],['week','Неделя'],['month','Месяц']].map(([key,label])=>`<option value="${key}" ${state.mode===key?'selected':''}>${label}</option>`).join('')}</select></div>${dates}`;
  }
  function dateStrip() {
    const start=monday(state.date);
    return `<div class="day-controls">${btn('shift','-1','‹','day-arrow')}<div class="dates" aria-label="Дни недели">${weekNames.map((name,i)=>{
      const date=shiftDay(start,i);
      return `<button type="button" data-day="${date}" aria-pressed="${date===state.date}" aria-label="${attr(formatDate(date))}"><small>${name}</small><span>${Number(date.slice(-2))}</span></button>`;
    }).join('')}</div>${btn('shift','1','›','day-arrow')}</div>`;
  }
  function scheduleViewport(content) {
    return `<div class="schedule-viewport" ${state.mode==='day'?'data-swipe-days="true"':''}><div class="schedule-content">${content}</div></div>`;
  }
  function freshness(values,refresh=true) {
    const rows=Array.isArray(values)?values:[values];
    const failed=rows.some(item=>item?.last_fetch_failed);
    const missing=rows.some(item=>!item?.has_snapshot);
    const times=rows.map(item=>item?.last_fetch_at).filter(Boolean).sort();
    const label=missing?'Расписание ещё не загружено':failed?'ДГТУ недоступен · сохранено '+stamp(times[0]):'Проверено '+stamp(times[0]);
    return `<div class="freshness ${failed||missing?'stale':''}"><span>${esc(label)}</span>${refresh?btn('refresh','1','Обновить','text-action'):''}</div>`;
  }
  function lessonCard(item,detail=false) {
    const tag=detail?'article':'button';
    const attrs=detail?'':`type="button" data-lesson="${attr(item.id)}" aria-label="${attr(item.start+'–'+item.end+', '+item.subject+', аудитория '+item.room)}"`;
    const statusName=item.deleted?'removed':item.status;
    const kind=Object.hasOwn(types,item.type)?item.type:'other';
    return `<${tag} ${attrs} class="lesson" data-kind="${kind}" data-status="${attr(statusName||'')}"><span class="lesson-top"><span><span class="time">${esc(item.start)}–${esc(item.end)}</span> <span class="lesson-meta">· ${types[kind]}${item.subgroup?' · п/г '+esc(item.subgroup):''}</span></span>${statuses[statusName]?`<span class="lesson-status">${statuses[statusName]}</span>`:''}</span><span class="subject">${esc(item.subject)}</span><span class="lesson-bottom"><span class="teacher-name">${esc(item.teacher||'Преподаватель не указан')}</span><span class="room">${esc(item.room||'—')}</span></span></${tag}>`;
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
  function scheduleBody(lessons,teacher=false) {
    if(state.mode==='month') return monthCalendar(lessons);
    return `<div class="${state.mode==='week'?'week-days':''}">${daysForMode().map(date=>{
      const dayLessons=lessons.filter(item=>item.date===date);
      const groups=teacher?[{id:'teacher',name:''}]:state.group==='all'?activeGroups():activeGroups().filter(group=>String(group.id)===state.group);
      return `<section class="day-section"><div class="heading"><h2>${esc(fullWeekNames[parseDate(date).getUTCDay()]+', '+formatDate(date))}</h2></div>${groups.map((group,index)=>{
        const filtered=teacher?dayLessons:dayLessons.filter(item=>String(item.group_id)===String(group.id));
        if(!teacher && state.group==='all') return groupBlock(group,filtered,index===0,date);
        if(teacher) return `<div class="agenda">${filtered.length?filtered.sort((a,b)=>a.start.localeCompare(b.start)).map(item=>`<div><p class="meta teacher-group-label">${esc(item.group_name)}</p>${lessonCard(item)}</div>`).join(''):'<div class="empty">Нет занятий</div>'}</div>`;
        return agenda(filtered);
      }).join('')}</section>`;
    }).join('')}</div>`;
  }
  function groupBlock(group,list,open=false,date=state.date) {
    const active=list.filter(item=>!item.deleted);
    const changed=list.filter(item=>item.status && item.status!=='baseline').length;
    const sorted=[...active].sort((a,b)=>a.start.localeCompare(b.start));
    const meta=active.length?active.every(item=>item.military)?'Военная кафедра':`${active.length} зан. · ${sorted[0].start}–${sorted.at(-1).end}`:'Нет занятий';
    return `<details class="group-block" data-block-key="${attr(group.id+':'+date)}" ${open?'open':''}><summary class="group-summary"><span><strong>${esc(group.name)}</strong><small>${esc(meta)}${changed?' · '+changed+' изм.':''}</small></span></summary><div class="group-body">${agenda(list)}</div></details>`;
  }
  function subjectShort(value) {
    const words=String(value).split(/\s+/).filter(Boolean);
    if(words.length<=2) return value;
    return words.filter(word=>word.length>2).slice(0,6).map(word=>word[0].toUpperCase()).join('');
  }
  function monthCalendar(lessons) {
    const groups=state.page==='teacher'?[{id:'teacher',name:''}]:state.group==='all'?activeGroups():activeGroups().filter(group=>String(group.id)===state.group);
    return groups.map((group,index)=>{
      const filtered=group.id==='teacher'?lessons:lessons.filter(item=>String(item.group_id)===String(group.id));
      const first=firstMonth(state.date),last=lastMonth(state.date);
      const start=monday(safeDay(first)),end=shiftDay(monday(last),5);
      const dates=[];
      for(let date=start;date<=end;date=shiftDay(date,1)) if(parseDate(date).getUTCDay()!==0) dates.push(date);
      const calendar=`<div class="calendar" aria-label="Расписание на месяц">${weekNames.map(name=>`<span class="calendar-label">${name}</span>`).join('')}${dates.map(date=>{
        if(date<first || date>last) return '<span class="calendar-gap" aria-hidden="true"></span>';
        const day=filtered.filter(item=>item.date===date&&!item.deleted).sort((a,b)=>a.start.localeCompare(b.start));
        const military=day.length && day.every(item=>item.military);
        const label=formatDate(date)+', '+(military?'Военная кафедра':day.length?day.map(item=>item.start+' '+item.subject).join(', '):'нет занятий');
        return `<button type="button" class="calendar-cell" data-month-day="${date}" data-current="${date===state.bootstrap.today}" aria-label="${attr(label)}"><time datetime="${date}">${Number(date.slice(-2))}</time>${military?'<span class="month-item" data-kind="lec">Военная кафедра</span>':day.length?day.slice(0,3).map(item=>`<span class="month-item" data-kind="${attr(item.type)}">${esc(item.start)} ${esc(subjectShort(item.subject))}</span>`).join(''):'<span class="more" aria-hidden="true">—</span>'}${!military && day.length>3?`<span class="more">ещё ${day.length-3}</span>`:''}</button>`;
      }).join('')}</div>`;
      return state.group==='all' && state.page!=='teacher'?`<details class="group-block" data-block-key="${attr(group.id+':'+firstMonth(state.date))}" ${index===0?'open':''}><summary class="group-summary"><strong>${esc(group.name)}</strong></summary><div class="group-body">${calendar}</div></details>`:calendar;
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
    return `<div class="event" data-event="${attr(type)}">${link?`<button type="button" class="feed-link" data-history="${attr(event.lesson_id)}"><strong>${esc(title)}</strong><small>${esc(event.group_name)} · ${esc(item?.date?formatDate(item.date):'')} · ${esc(item?.subject||'Занятие')}</small></button>`:`<strong>${esc(title)}</strong>`}<time>${esc(stamp(event.detected_at))} · МСК</time>${body}</div>`;
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
    return `${btn('back',state.localDetail?'teacher':state.returnPage,'‹ Назад','back')}<div class="context"><h1>${esc(item.group_name)} · ${esc(formatDate(item.date))}</h1><p>${esc(item.deleted?'Удалённая пара':'Подробности занятия')}</p></div><div class="detail">${lessonCard(item,true)}</div><div class="detail-actions">${btn('detail-tab','details','Преподаватель',state.detailTab==='details'?'action primary':'action')}${btn('detail-tab','history','История',state.detailTab==='history'?'action primary':'action')}</div>${state.detailTab==='history'?historyView(detail):`${btn(state.localDetail?'back':'teacher-open',state.localDetail?'teacher':item.id,`${avatar(teacher)}<span class="person-text"><strong>${esc(item.teacher||'Преподаватель не указан')}</strong><small>Открыть расписание преподавателя</small></span><span class="person-arrow">›</span>`,'person')}<p class="settings-note">Двойное нажатие на пару сразу открывает её историю</p>`}`;
  }
  function settingsView() {
    const user=profile(),fields=state.bootstrap,disabled=state.busy?'disabled':'';
    const check=(key,label,description='')=>`<label class="setting"><span class="setting-label">${esc(label)}${description?`<small>${esc(description)}</small>`:''}</span><input type="checkbox" data-save="${key}" ${user[key]?'checked':''} ${disabled}></label>`;
    const disclosure=(key,title,summary,body)=>`<details class="settings-block" data-settings-section="${key}" ${state.settingsOpen[key]?'open':''}><summary><span><strong>${esc(title)}</strong><small>${esc(summary)}</small></span><span class="settings-chevron" aria-hidden="true">⌄</span></summary><div class="settings-block-body">${body}</div></details>`;
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
  function render() {
    app.dataset.season=season();
    app.dataset.cardStyle=profile().miniapp_card_style||'stripe';
    navigation.hidden=!state.bootstrap;
    navigation.querySelectorAll('[data-page]').forEach(button=>{
      const active=button.dataset.page===(['detail','teacher'].includes(state.page)?state.returnPage:state.page);
      if(active) button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
    });
    try {if(['detail','teacher'].includes(state.page)) telegram?.BackButton?.show();else telegram?.BackButton?.hide();} catch(_) {}
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
    if(state.page==='teacher' && state.teacher) {
      const result=state.teacher;
      if(result.candidates?.length && !result.teacher) {
        view.innerHTML=btn('back','detail','‹ К паре','back')+header('Преподаватель')+'<p class="meta">Найдено несколько преподавателей. Выберите нужного.</p>'+result.candidates.map(person=>btn('teacher-choice',person.id,`${avatar(person)}<span class="person-text"><strong>${esc(person.name)}</strong></span><span class="person-arrow">›</span>`,'person')).join('');return;
      }
      if(!result.teacher) {view.innerHTML=btn('back','detail','‹ К паре','back')+`<div class="empty">${esc(result.message||'Преподаватель не найден в ДГТУ')}</div>`;return;}
      view.innerHTML=btn('back','detail','‹ К паре','back')+`<header class="profile">${avatar(result.teacher)}<h1>${esc(result.teacher.name)}</h1></header>`+toolbar(true)+freshness(result.freshness,false)+scheduleViewport(result.loading?'<p class="busy">Загрузка расписания…</p>':scheduleBody(result.lessons,true));return;
    }
    if(state.page==='changes') {
      view.innerHTML=header('Изменения')+'<p class="meta">История отслеживаемых групп · время обнаружения ботом</p>'+`<div class="feed">${state.events.length?state.events.map(event=>historyEvent(event,true)).join(''):'<div class="empty">Изменений пока нет<p>Журнал заполняется при проверках расписания</p></div>'}</div>`+(state.nextOffset!==null?btn('more-events',state.nextOffset,'Показать ещё','action block-button'):'');return;
    }
    if(state.page==='schedule' && state.bootstrap) {
      const opened=new Map([...view.querySelectorAll('[data-block-key]')].map(section=>[section.dataset.blockKey,section.open]));
      const month=formatDate(state.date,{month:'long',year:'numeric'}).replace(/ г\.$/,'');
      view.innerHTML=header(month[0].toLocaleUpperCase('ru-RU')+month.slice(1),true)+toolbar()+(state.schedule?freshness(state.schedule.freshness):'')+scheduleViewport(state.schedule?scheduleBody(state.schedule.lessons):'<p class="busy">Загрузка расписания…</p>');
      view.querySelectorAll('[data-block-key]').forEach(section=>{if(opened.has(section.dataset.blockKey)) section.open=opened.get(section.dataset.blockKey);});
      return;
    }
    view.innerHTML=header(state.page==='detail'?'Занятие':state.page==='teacher'?'Преподаватель':state.page==='changes'?'Изменения':'Расписание')+'<p class="busy">Загрузка…</p>';
  }
  function applyBootstrap(response) {
    const roster=JSON.stringify(response.groups.map(group=>[group.id,group.primary,group.enabled,group.last_fetch_at,group.last_fetch_failed]));
    if(rosterVersion && rosterVersion!==roster) clearScheduleCache();
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
    if(pageMotion) {pageMotion.cancel();pageMotion=null;}
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
        else if(page==='schedule') state.schedule=null;
        else if(state.teacher?.teacher) state.teacher={...state.teacher,lessons:[],loading:true};
        else state.teacher=null;
        if(cached && !options.force && Date.now()-cached.at<60000) return;
        render();
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
        render();
        const result=await api('/api/changes?'+new URLSearchParams({limit:50,offset:options.offset||0}));
        if(version!==requestVersion) return;state.events=options.append?[...state.events,...result.events]:result.events;state.nextOffset=result.next_offset;
      } else if(page==='settings') {
        await bootstrap();
      }
    } catch(error) {
      if(version===requestVersion && !options.silent) {
        if(cached && ![401,403].includes(error.status)) notify('Нет связи с сервером · показана сохранённая версия');
        else state.error=error;
      }
    }
    finally {if(version===requestVersion) {setBusy(false);render();animateSchedule(options.direction);}}
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
  function animateSchedule(direction) {
    const content=view.querySelector('.schedule-content');
    if(!direction || !content || state.mode!=='day' || reduceMotion() || !content.animate) return;
    pageMotion=content.animate([
      {transform:`translate3d(${direction*36}px,0,0)`,opacity:.45},
      {transform:'translate3d(0,0,0)',opacity:1}
    ],{duration:180,easing:'cubic-bezier(.22,1,.36,1)'});
  }
  async function shiftSchedule(amount) {
    if(state.mode==='month') state.date=shiftMonth(state.date,amount);
    else if(state.mode==='week') state.date=shiftDay(state.date,7*amount);
    else {
      state.date=shiftDay(state.date,amount);
      if(parseDate(state.date).getUTCDay()===0) state.date=shiftDay(state.date,amount);
    }
    await loadPage({direction:amount});
  }
  function cancelGesture(bounce=true) {
    const current=gesture;gesture=null;
    if(!current) return;
    const transform=current.content.style.transform;
    current.content.style.transform='';current.content.classList.remove('is-dragging');
    try {if(current.viewport.hasPointerCapture(current.id)) current.viewport.releasePointerCapture(current.id);} catch(_) {}
    if(bounce && current.axis==='x' && !reduceMotion() && current.content.animate) {
      current.content.animate([{transform},{transform:'translate3d(0,0,0)'}],{duration:140,easing:'ease-out'});
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
    cancelGesture(!accepted);
    if(accepted) shiftSchedule(dx<0?1:-1);
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
    if(document.visibilityState==='hidden' || syncing || gesture || state.busy || tapTimer || !state.bootstrap || !['schedule','changes'].includes(state.page)) return;
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
    else if(target.dataset.teacherOpen) {state.page='teacher';state.teacherChoice='';state.date=state.detail.lesson.date;await loadPage();}
    else if(target.dataset.teacherChoice) {state.teacherChoice=target.dataset.teacherChoice;await loadPage();}
    else if(target.dataset.day) {const direction=target.dataset.day>state.date?1:-1;state.date=target.dataset.day;await loadPage({direction});}
    else if(target.dataset.monthDay) {state.date=target.dataset.monthDay;state.mode='day';await loadPage();}
    else if(target.dataset.shift) {
      await shiftSchedule(Number(target.dataset.shift));
    } else if(target.dataset.today) {state.date=safeDay(state.bootstrap.today);await loadPage();}
    else if(target.dataset.retry) await loadPage();
    else if(target.dataset.moreEvents) await loadPage({offset:target.dataset.moreEvents,append:true,keep:true});
    else if(target.dataset.refresh) {if(await queuedAction('/api/refresh',{group_id:state.group})) await loadPage();}
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
    if(target.hasAttribute('data-group')) {state.group=target.value;await loadPage();}
    else if(target.hasAttribute('data-mode')) {state.mode=target.value;await loadPage();}
    else if(target.hasAttribute('data-date')) {
      const value=target.value.length===7?target.value+'-01':target.value;
      if(/^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(parseDate(value).valueOf())) {state.date=safeDay(value);await loadPage();}
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
  telegram.onEvent?.('themeChanged',syncTheme);
  telegram.BackButton?.onClick(navigateBack);
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible') syncLive();});
  try {if(!telegram.isVersionAtLeast || telegram.isVersionAtLeast('8.0')) telegram.onEvent?.('activated',syncLive);} catch(_) {}
  setInterval(syncLive,60000);
  loadPage();
})();
