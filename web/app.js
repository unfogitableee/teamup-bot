const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const $ = (id) => document.getElementById(id);
const state = {
  tab: "projects",
  mode: "all",
  projectScope: "all",
  peopleScope: "all",
  inboxScope: "incoming",
  stack: "",
  me: null,
  config: { bot_username: "tteamup_bot" },
  access: null,
  people: [],
  ownProjects: [],
  detail: null,
  onboardingGoal: "both",
};

const modeLabels = { free:"🟢 Бесплатно", paid:"💼 Оплата", share:"💎 Доля" };
const statusLabels = { pending:"⏳ Ждёт решения", accepted:"✅ Принято", declined:"❌ Отклонено" };
const stacks = ["Python","React","TypeScript","FastAPI","Django","Figma","Flutter","Kotlin","Swift","Unity","QA","DevOps","Marketing","Sales"];

function authHeaders(){
  const h={"Content-Type":"application/json"};
  if(tg?.initData) h["X-Telegram-Init-Data"]=tg.initData;
  return h;
}
async function api(url,options={}){
  options.headers={...authHeaders(),...(options.headers||{})};
  const r=await fetch(url,options);
  if(!r.ok){let m="Ошибка";try{m=(await r.json()).detail||m}catch{}throw new Error(m)}
  return r.json();
}
function esc(v=""){return String(v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function tagHtml(csv=""){return csv.split(",").map(x=>x.trim()).filter(Boolean).map(x=>`<span class="tag">${esc(x)}</span>`).join("")}
function toast(text,type="success"){
  const el=$("toast");el.textContent=text;el.classList.add("show");clearTimeout(toast.t);toast.t=setTimeout(()=>el.classList.remove("show"),1700);
  tg?.HapticFeedback?.notificationOccurred?.(type==="error"?"error":"success");
}
function openUser(username){if(!username)return;const url=`https://t.me/${username}`;tg?.openTelegramLink?tg.openTelegramLink(url):window.open(url,"_blank")}
function modal(id,show=true){$(id).classList.toggle("hidden",!show)}
function projectMine(p){return state.me&&Number(p.owner_id)===Number(state.me.telegram_id)}

function projectCard(p){
  const mine=projectMine(p),closed=p.status==="closed";
  return `<article class="card ${closed?"closed":""}">
    <div class="card-top"><h3>${esc(p.title)}</h3><div class="card-badges">${p.match_score!=null?`<span class="match-badge">✨ ${p.match_score}%</span>`:""}<span class="mode">${modeLabels[p.mode]}</span></div></div>
    <div class="desc">${esc(p.description)}</div>
    <div class="tags">${tagHtml(p.stack)}</div>
    ${p.match_reasons?.length?`<div class="match-line">${p.match_reasons.map(esc).join(" · ")}</div>`:""}
    <div class="meta">
      <div>Ищут<b>${esc(p.roles||"Участников")}</b></div><div>Занятость<b>${esc(p.hours||"Обсудим")}</b></div>
      <div>Условия<b>${esc(p.terms||modeLabels[p.mode])}</b></div><div>${mine?"Отклики":"Автор"}<b>${mine?`${p.pending_applications||0} новых`:esc(p.owner_name||"Участник")}</b></div>
    </div>
    <div class="card-actions">
      <button class="primary" onclick="openProjectDetail(${p.id})">Подробнее</button>
      ${mine?`<button class="secondary" onclick="editProject(${p.id})">Редактировать</button>`:`<button class="secondary" onclick="openApplyModal(${p.id})" ${closed?"disabled":""}>Откликнуться</button>`}
      ${closed?`<span class="mini-badge">Закрыт</span>`:""}
    </div>
  </article>`;
}

function personCard(p){
  const modes=[p.mode_free?"🟢 бесплатно":"",p.mode_paid?"💼 оплата":"",p.mode_share?"💎 доля":""].filter(Boolean).join(" · ");
  return `<article class="card person">
    <div class="avatar">${esc((p.first_name||"👤")[0])}</div>
    <div class="grow">
      <div class="card-top"><h3>${esc(p.first_name||"Участник")}</h3><div class="card-badges">${p.match_score!=null?`<span class="match-badge">✨ ${p.match_score}%</span>`:""}<span class="mini-badge">${esc(p.level||"")}</span></div></div>
      <div class="muted">${esc(p.role||"Роль не указана")}</div>
      <div class="tags" style="margin-top:8px">${tagHtml(p.stack)}</div>
      ${p.match_reasons?.length?`<div class="match-line">${p.match_reasons.map(esc).join(" · ")}</div>`:""}
      <div class="desc">${esc(p.bio||"Открыт к интересным предложениям.")}</div>
      <div class="score">${modes||"Формат не указан"}${p.availability?` · ${esc(p.availability)}`:""}</div>
      <div class="card-actions">
        <button class="primary" onclick="openInviteModal(${p.telegram_id})">Пригласить</button>
        ${p.username?`<button class="secondary" onclick="openUser('${esc(p.username)}')">Написать</button>`:""}
        <button class="ghost-danger" onclick="openReport('user',${p.telegram_id},'${esc(p.first_name||"пользователя")}')">Пожаловаться</button>
      </div>
    </div>
  </article>`;
}

async function loadConfig(){try{state.config=await api("/api/config")}catch{}}
async function checkAccess(){
  try{state.access=await api("/api/access")}catch(e){$("accessGate").classList.remove("hidden");$("gateStatus").textContent=e.message;return false}
  if(!state.access.required||state.access.subscribed){$("accessGate").classList.add("hidden");return true}
  $("accessGate").classList.remove("hidden");return false;
}
async function loadMe(){
  state.me=await api("/api/me");
  $("profileName").textContent=state.me.first_name||"Профиль";$("avatar").textContent=(state.me.first_name||"🙂")[0];
  const f=$("profileForm");f.role.value=state.me.role||"";f.level.value=state.me.level||"";f.stack.value=state.me.stack||"";f.availability.value=state.me.availability||"";f.bio.value=state.me.bio||"";f.seeking_status.value=state.me.seeking_status||"open";f.goal.value=state.me.goal||"both";f.mode_free.checked=!!state.me.mode_free;f.mode_paid.checked=!!state.me.mode_paid;f.mode_share.checked=!!state.me.mode_share;
  const badge=$("seekingBadge");
  const incomplete=!profileReady();
  badge.textContent=incomplete?"Регистрация не завершена":(state.me.seeking_status==="busy"?"Пока не ищу":"Открыт к предложениям");
  badge.classList.toggle("busy",incomplete||state.me.seeking_status==="busy");
  $("adminBtn")?.classList.toggle("hidden",!state.me.is_admin);
}

function renderStacks(){
  $("quickStacks").innerHTML=stacks.map(s=>`<button class="chip ${state.stack===s?"active":""}" data-stack="${s}">${s}</button>`).join("");
  document.querySelectorAll("[data-stack]").forEach(b=>b.onclick=()=>{state.stack=state.stack===b.dataset.stack?"":b.dataset.stack;$("stackSearch").value=state.stack;renderStacks();loadFeed()});
}

async function loadFeed(){
  if(!["projects","people"].includes(state.tab))return;
  $("feed").innerHTML='<div class="empty">Загружаю…</div>';
  try{
    if(state.tab==="projects"){
      const q=new URLSearchParams();
      if(state.mode!=="all")q.set("mode",state.mode);
      if(state.stack)q.set("stack",state.stack);
      if(state.projectScope==="mine"){q.set("mine","true");q.set("include_closed","true")}
      if(state.projectScope==="recommended")q.set("recommended","true");
      const data=await api(`/api/projects?${q}`);
      $("feed").innerHTML=data.length?data.map(projectCard).join(""):'<div class="empty">Здесь пока пусто.<br>Создай первый проект.</div>';
    }else{
      const q=new URLSearchParams();
      if(state.mode!=="all")q.set("mode",state.mode);
      if(state.stack)q.set("stack",state.stack);
      if(state.peopleScope==="recommended")q.set("recommended","true");
      state.people=await api(`/api/people?${q}`);
      $("feed").innerHTML=state.people.length?state.people.map(personCard).join(""):'<div class="empty"><b>Пока никого подходящего.</b><br>Если это первый запуск — всё нормально: реальные люди появятся, когда начнёшь звать первых участников.</div>';
    }
  }catch(e){$("feed").innerHTML=`<div class="empty">${esc(e.message)}</div>`}
}

function setTab(tab){
  state.tab=tab;document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));
  $("discoverScreen").classList.toggle("hidden",!["projects","people"].includes(tab));$("inboxScreen").classList.toggle("hidden",tab!=="inbox");$("profileScreen").classList.toggle("hidden",tab!=="profile");$("createBtn").classList.toggle("hidden",tab!=="projects");
  $("projectScope").classList.toggle("hidden",tab!=="projects");
  $("peopleScope").classList.toggle("hidden",tab!=="people");
  $("screenTitle").textContent={projects:"Проекты",people:"Люди",inbox:"Отклики",profile:"Профиль"}[tab];
  if(["projects","people"].includes(tab))loadFeed();if(tab==="inbox")loadInbox();if(tab==="profile")loadMe();
}

async function openProjectDetail(id){
  try{
    const p=await api(`/api/projects/${id}`);state.detail=p;$("detailTitle").textContent=p.title;
    const ownerButtons=p.owner_username?`<button class="secondary" onclick="openUser('${esc(p.owner_username)}')">Написать автору</button>`:"";
    const mine=p.is_owner;
    let actions="";
    if(mine){actions=`<button class="primary" onclick="editProject(${p.id});modal('detailModal',false)">Редактировать</button><button class="secondary" onclick="toggleProjectStatus(${p.id},'${p.status==='open'?'closed':'open'}')">${p.status==='open'?'Закрыть проект':'Открыть снова'}</button>`}
    else if(p.status==="open"){
      actions=p.my_application_status?`<span class="status ${p.my_application_status}">${statusLabels[p.my_application_status]}</span>${ownerButtons}`:`<button class="primary" onclick="openApplyModal(${p.id})">Откликнуться</button>${ownerButtons}`;
    }
    $("detailBody").innerHTML=`<div class="detail-hero"><div class="card-top"><span class="mode">${modeLabels[p.mode]}</span><span class="mini-badge">${p.status==='open'?'Открыт':'Закрыт'}</span></div><div class="detail-desc" style="margin-top:12px">${esc(p.description)}</div><div class="tags" style="margin-top:12px">${tagHtml(p.stack)}</div></div>
      <div class="detail-grid"><div class="detail-cell"><span>Ищут</span><b>${esc(p.roles||'Участников')}</b></div><div class="detail-cell"><span>Занятость</span><b>${esc(p.hours||'Обсудим')}</b></div><div class="detail-cell"><span>Условия</span><b>${esc(p.terms||modeLabels[p.mode])}</b></div><div class="detail-cell"><span>Автор</span><b>${esc(p.owner_name||'Участник')}</b></div></div>
      <div class="card-actions">${actions}<button class="secondary" onclick="shareProject(${p.id},${JSON.stringify(p.title).replace(/"/g,'&quot;')})">Поделиться</button>${mine?"":`<button class="ghost-danger" onclick="openReport('project',${p.id},'${esc(p.title)}')">Пожаловаться</button>`}</div>`;
    modal("detailModal",true);
    recordValueView();
  }catch(e){toast(e.message,"error")}
}
window.openProjectDetail=openProjectDetail;

function openCreateProject(){const f=$("projectForm");f.reset();f.project_id.value="";$("projectFormTitle").textContent="Новый проект";$("projectSubmitBtn").textContent="Опубликовать";modal("projectModal",true)}
async function editProject(id){
  try{const p=await api(`/api/projects/${id}`),f=$("projectForm");f.project_id.value=p.id;f.title.value=p.title;f.description.value=p.description;f.mode.value=p.mode;f.roles.value=p.roles;f.stack.value=p.stack;f.terms.value=p.terms;f.hours.value=p.hours;$("projectFormTitle").textContent="Редактировать проект";$("projectSubmitBtn").textContent="Сохранить";modal("projectModal",true)}catch(e){toast(e.message,"error")}
}
window.editProject=editProject;
async function toggleProjectStatus(id,status){try{await api(`/api/projects/${id}/status?status=${status}`,{method:"POST"});toast(status==="closed"?"Проект закрыт":"Проект снова открыт");modal("detailModal",false);loadFeed()}catch(e){toast(e.message,"error")}}
window.toggleProjectStatus=toggleProjectStatus;

async function openApplyModal(id){
  try{const p=state.detail?.id===id?state.detail:await api(`/api/projects/${id}`);$("applyTitle").textContent=p.title;const f=$("applyForm");f.project_id.value=id;f.message.value="";modal("detailModal",false);modal("applyModal",true)}catch(e){toast(e.message,"error")}
}
window.openApplyModal=openApplyModal;

async function getOwnOpenProjects(){state.ownProjects=await api("/api/projects?mine=true");return state.ownProjects}
async function openInviteModal(recipientId){
  try{const projects=await getOwnOpenProjects();if(!projects.length){toast("Сначала создай открытый проект","error");setTab("projects");state.projectScope="mine";syncScope();return}
    const person=state.people.find(x=>Number(x.telegram_id)===Number(recipientId));$("inviteTitle").textContent=person?`Пригласить ${person.first_name}`:"Пригласить в проект";const f=$("inviteForm");f.recipient_id.value=recipientId;$("inviteProjectSelect").innerHTML=projects.map(p=>`<option value="${p.id}">${esc(p.title)}</option>`).join("");f.message.value="";modal("inviteModal",true)
  }catch(e){toast(e.message,"error")}
}
window.openInviteModal=openInviteModal;

function shareProject(id,title){
  const botName=state.config?.bot_username||"tteamup_bot";const url=`https://t.me/${botName}?startapp=project_${id}`;const text=`🚀 ${title}\n\nНашёл проект в Тимап:`;const share=`https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`;tg?.openTelegramLink?tg.openTelegramLink(share):window.open(share,"_blank");
}
window.shareProject=shareProject;window.openUser=openUser;


function openReport(type,id,label=""){
  const f=$("reportForm");
  f.target_type.value=type;
  f.target_id.value=id;
  f.reason.value="";
  $("reportTitle").textContent=type==="project"?`Пожаловаться на проект`:`Пожаловаться на пользователя`;
  modal("reportModal",true);
}
window.openReport=openReport;

function inboxAppCard(a,incoming){
  return `<article class="card ${a.status}"><div class="card-top"><h3>${esc(a.project_title)}</h3><span class="status ${a.status}">${statusLabels[a.status]}</span></div>
    <div class="desc">${incoming?`<b>${esc(a.first_name)}</b>${a.role?` · ${esc(a.role)}`:""}${a.message?`\n\n${esc(a.message)}`:"\n\nБез сообщения."}`:`${modeLabels[a.mode]}${a.status==='accepted'?`\n\nАвтор: ${esc(a.owner_name||'Участник')}`:""}`}</div>
    ${incoming?`<div class="tags">${tagHtml(a.stack)}</div>`:""}
    <div class="card-actions">${incoming&&a.status==='pending'?`<button class="primary" onclick="decideApplication(${a.id},'accepted')">Принять</button><button class="danger" onclick="decideApplication(${a.id},'declined')">Отказать</button>`:""}${incoming&&a.username?`<button class="secondary" onclick="openUser('${esc(a.username)}')">Написать</button>`:""}${!incoming&&a.status==='accepted'&&a.owner_username?`<button class="primary" onclick="openUser('${esc(a.owner_username)}')">Написать автору</button>`:""}<button class="secondary" onclick="openProjectDetail(${a.project_id})">Проект</button></div></article>`;
}
function inboxInviteCard(i,incoming){
  return `<article class="card ${i.status}"><div class="card-top"><h3>${esc(i.project_title)}</h3><span class="status ${i.status}">${statusLabels[i.status]}</span></div>
    <div class="desc">${incoming?`Тебя приглашает <b>${esc(i.sender_name)}</b>${i.message?`\n\n${esc(i.message)}`:""}`:`Приглашение для <b>${esc(i.recipient_name)}</b>${i.role?` · ${esc(i.role)}`:""}`}</div>
    ${incoming?`<div class="tags">${tagHtml(i.stack)}</div><div class="meta"><div>Ищут<b>${esc(i.roles)}</b></div><div>Условия<b>${esc(i.terms)}</b></div></div>`:`<div class="tags">${tagHtml(i.stack)}</div>`}
    <div class="card-actions">${incoming&&i.status==='pending'?`<button class="primary" onclick="decideInvitation(${i.id},'accepted')">Принять</button><button class="danger" onclick="decideInvitation(${i.id},'declined')">Отказать</button>`:""}${incoming&&i.status==='accepted'&&i.sender_username?`<button class="primary" onclick="openUser('${esc(i.sender_username)}')">Написать автору</button>`:""}${!incoming&&i.status==='accepted'&&i.recipient_username?`<button class="primary" onclick="openUser('${esc(i.recipient_username)}')">Написать</button>`:""}<button class="secondary" onclick="openProjectDetail(${i.project_id})">Проект</button></div></article>`;
}
async function loadInbox(){
  $("inboxContent").innerHTML='<div class="empty">Загружаю…</div>';
  try{const d=await api("/api/inbox");if(state.inboxScope==="incoming"){$("inboxContent").innerHTML=`<div class="section-title">Приглашения в проекты</div>${d.incoming_invitations.length?`<div class="feed">${d.incoming_invitations.map(x=>inboxInviteCard(x,true)).join("")}</div>`:'<div class="empty">Приглашений пока нет.</div>'}<div class="section-title">Отклики на мои проекты</div>${d.incoming_applications.length?`<div class="feed">${d.incoming_applications.map(x=>inboxAppCard(x,true)).join("")}</div>`:'<div class="empty">Новых откликов пока нет.</div>'}`}else{$("inboxContent").innerHTML=`<div class="section-title">Мои отклики</div>${d.outgoing_applications.length?`<div class="feed">${d.outgoing_applications.map(x=>inboxAppCard(x,false)).join("")}</div>`:'<div class="empty">Ты пока никуда не откликался.</div>'}<div class="section-title">Отправленные приглашения</div>${d.outgoing_invitations.length?`<div class="feed">${d.outgoing_invitations.map(x=>inboxInviteCard(x,false)).join("")}</div>`:'<div class="empty">Ты пока никого не приглашал.</div>'}`}}
  catch(e){$("inboxContent").innerHTML=`<div class="empty">${esc(e.message)}</div>`}
}
async function decideApplication(id,status){try{await api(`/api/applications/${id}/decision`,{method:"POST",body:JSON.stringify({status})});toast(status==="accepted"?"Участник принят":"Отклик отклонён");loadInbox()}catch(e){toast(e.message,"error")}}
async function decideInvitation(id,status){try{await api(`/api/invitations/${id}/decision`,{method:"POST",body:JSON.stringify({status})});toast(status==="accepted"?"Приглашение принято":"Приглашение отклонено");loadInbox()}catch(e){toast(e.message,"error")}}
window.decideApplication=decideApplication;window.decideInvitation=decideInvitation;

function profilePayload(form){return {role:form.role.value,level:form.level.value,bio:form.bio.value,stack:form.stack.value.split(",").map(x=>x.trim()).filter(Boolean),availability:form.availability.value,mode_free:form.mode_free.checked,mode_paid:form.mode_paid.checked,mode_share:form.mode_share.checked,seeking_status:form.seeking_status.value,goal:form.goal.value||"both"}}

function syncScope(){document.querySelectorAll("[data-scope]").forEach(b=>b.classList.toggle("active",b.dataset.scope===state.projectScope));loadFeed()}

function profileReady(){
  return !!(state.me?.role?.trim()&&state.me?.stack?.trim());
}

function syncGoalButtons(){
  document.querySelectorAll("[data-goal]").forEach(b=>{
    b.classList.toggle("active",b.dataset.goal===state.onboardingGoal);
  });
}

function showFirstVisitRegistration(){
  state.onboardingGoal=state.me?.goal||"both";
  $("obRole").value=state.me?.role||"";
  $("obStack").value=state.me?.stack||"";
  syncGoalButtons();
  modal("onboardingModal",true);
  setTimeout(()=>$("obRole")?.focus(),180);
}

function channelHandle(){
  return state.config?.channel_username||state.config?.autopost_channel||state.config?.required_channel||"";
}

function hideChannelBanner(permanent=true){
  $("channelBanner")?.classList.add("hidden");
  if(permanent){
    try{localStorage.setItem("teamup_channel_offer_seen","1")}catch{}
  }
}

function maybeShowChannelBanner(){
  const channel=channelHandle();
  if(!channel||!profileReady())return;
  try{
    if(localStorage.getItem("teamup_channel_offer_seen")==="1")return;
    const views=Number(localStorage.getItem("teamup_value_views")||"0");
    if(views<3)return;
  }catch{return}
  $("channelBanner")?.classList.remove("hidden");
}

function recordValueView(){
  try{
    const views=Number(localStorage.getItem("teamup_value_views")||"0")+1;
    localStorage.setItem("teamup_value_views",String(Math.min(views,20)));
  }catch{}
  maybeShowChannelBanner();
}

function openChannel(){
  const channel=channelHandle();
  if(!channel)return;
  const url=`https://t.me/${channel.replace(/^@/,"")}`;
  hideChannelBanner(true);
  tg?.openTelegramLink?tg.openTelegramLink(url):window.open(url,"_blank");
}

async function finishOnboarding(){
  const role=$("obRole").value.trim();
  const stack=$("obStack").value.split(",").map(x=>x.trim()).filter(Boolean);

  if(!role){
    toast("Выбери роль или напиши свою","error");
    return;
  }
  if(!stack.length){
    toast("Укажи хотя бы один навык или технологию","error");
    return;
  }

  const payload={
    role,
    level:state.me?.level||"",
    bio:state.me?.bio||"",
    stack,
    availability:state.me?.availability||"",
    mode_free:true,
    mode_paid:true,
    mode_share:true,
    seeking_status:"open",
    goal:state.onboardingGoal||"both"
  };

  try{
    await api("/api/profile",{method:"POST",body:JSON.stringify(payload)});
    await loadMe();
    modal("onboardingModal",false);

    state.projectScope="recommended";
    state.peopleScope="recommended";
    document.querySelectorAll("[data-scope]").forEach(b=>b.classList.toggle("active",b.dataset.scope===state.projectScope));
    document.querySelectorAll("[data-people-scope]").forEach(b=>b.classList.toggle("active",b.dataset.peopleScope===state.peopleScope));

    const target=payload.goal==="people"?"people":"projects";
    setTab(target);
    toast("Готово — добро пожаловать в Тимап");
  }catch(e){
    toast(e.message,"error");
  }
}

// Events
$("createBtn").onclick=openCreateProject;
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
document.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>{state.mode=b.dataset.mode;document.querySelectorAll("[data-mode]").forEach(x=>x.classList.toggle("active",x===b));loadFeed()});
document.querySelectorAll("[data-scope]").forEach(b=>b.onclick=()=>{state.projectScope=b.dataset.scope;syncScope()});
document.querySelectorAll("[data-inbox]").forEach(b=>b.onclick=()=>{state.inboxScope=b.dataset.inbox;document.querySelectorAll("[data-inbox]").forEach(x=>x.classList.toggle("active",x===b));loadInbox()});
document.querySelectorAll("[data-close]").forEach(b=>b.onclick=()=>modal(b.dataset.close,false));
document.querySelectorAll(".modal").forEach(m=>m.addEventListener("click",e=>{
  if(e.target===m&&!m.classList.contains("onboarding-modal"))modal(m.id,false)
}));
$("stackSearch").addEventListener("input",e=>{state.stack=e.target.value.trim();renderStacks();clearTimeout(window.searchTimer);window.searchTimer=setTimeout(loadFeed,220)});$("clearSearch").onclick=()=>{state.stack="";$("stackSearch").value="";renderStacks();loadFeed()};
$("profileForm").onsubmit=async e=>{e.preventDefault();try{await api("/api/profile",{method:"POST",body:JSON.stringify(profilePayload(e.currentTarget))});await loadMe();toast("Профиль сохранён")}catch(err){toast(err.message,"error")}};
$("projectForm").onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,payload={title:f.title.value,description:f.description.value,mode:f.mode.value,stack:f.stack.value.split(",").map(x=>x.trim()).filter(Boolean),roles:f.roles.value,terms:f.terms.value,hours:f.hours.value};try{const id=Number(f.project_id.value||0);await api(id?`/api/projects/${id}`:"/api/projects",{method:id?"PUT":"POST",body:JSON.stringify(payload)});modal("projectModal",false);toast(id?"Проект обновлён":"Проект опубликован");state.projectScope="mine";state.mode=payload.mode;document.querySelectorAll("[data-mode]").forEach(x=>x.classList.toggle("active",x.dataset.mode===state.mode));syncScope()}catch(err){toast(err.message,"error")}};
$("applyForm").onsubmit=async e=>{e.preventDefault();const f=e.currentTarget;try{await api(`/api/projects/${Number(f.project_id.value)}/apply`,{method:"POST",body:JSON.stringify({message:f.message.value})});modal("applyModal",false);toast("Отклик отправлен")}catch(err){toast(err.message,"error")}};
$("reportForm").onsubmit=async e=>{e.preventDefault();const f=e.currentTarget;try{await api("/api/reports",{method:"POST",body:JSON.stringify({target_type:f.target_type.value,target_id:Number(f.target_id.value),reason:f.reason.value.trim()})});modal("reportModal",false);toast("Жалоба отправлена администратору")}catch(err){toast(err.message,"error")}};

$("inviteForm").onsubmit=async e=>{e.preventDefault();const f=e.currentTarget;try{await api("/api/invitations",{method:"POST",body:JSON.stringify({project_id:Number(f.project_id.value),recipient_id:Number(f.recipient_id.value),message:f.message.value})});modal("inviteModal",false);toast("Приглашение отправлено")}catch(err){toast(err.message,"error")}};
$("subscribeBtn").onclick=()=>{const ch=state.access?.channel||channelHandle();if(!ch)return;const url=`https://t.me/${ch.replace(/^@/,"")}`;tg?.openTelegramLink?tg.openTelegramLink(url):window.open(url,"_blank")};
$("checkSubBtn").onclick=async()=>{$("gateStatus").textContent="Проверяю…";if(await checkAccess()){toast("Подписка подтверждена");await bootContent()}else $("gateStatus").textContent="Подписка пока не найдена."};
document.querySelectorAll("[data-role]").forEach(b=>b.onclick=()=>{$("obRole").value=b.dataset.role;document.querySelectorAll("[data-role]").forEach(x=>x.classList.toggle("active",x===b))});
document.querySelectorAll("[data-goal]").forEach(b=>b.onclick=()=>{
  state.onboardingGoal=b.dataset.goal;
  syncGoalButtons();
});
$("quickOnboardingForm").onsubmit=async e=>{e.preventDefault();await finishOnboarding()};
$("channelSubscribeBtn")?.addEventListener("click",openChannel);
$("channelDismissBtn")?.addEventListener("click",()=>hideChannelBanner(true));
$("channelBannerClose")?.addEventListener("click",()=>hideChannelBanner(true));

async function openDeepLink(){
  let id=null;const sp=tg?.initDataUnsafe?.start_param;if(sp&&sp.startsWith("project_"))id=Number(sp.slice(8));const qp=new URLSearchParams(location.search);if(qp.get("project"))id=Number(qp.get("project"));if(id)await openProjectDetail(id);
}

async function loadAdmin(){
  try{
    const [stats,reports,projects,users]=await Promise.all([
      api("/api/admin/stats"),
      api("/api/admin/reports?limit=30"),
      api("/api/admin/projects?limit=20"),
      api("/api/admin/users?limit=20"),
    ]);
    $("adminStats").innerHTML=`
      <div><span>Пользователи</span><b>${stats.users}</b><small>+${stats.new_users_24h} за 24ч</small></div>
      <div><span>Проекты</span><b>${stats.projects}</b><small>${stats.open_projects} открыто</small></div>
      <div><span>Отклики</span><b>${stats.applications}</b><small>${stats.pending_applications} ждут</small></div>
      <div><span>Приглашения</span><b>${stats.invitations}</b><small>всего</small></div>
      <div><span>Жалобы</span><b>${stats.pending_reports}</b><small>ждут разбора</small></div>`;
    $("adminReports").innerHTML=reports.length?reports.map(r=>`
      <div class="admin-row report-row ${r.status!=="pending"?"muted-row":""}">
        <div class="grow">
          <b>${r.target_type==="project"?"🚀 Проект":"👤 Пользователь"}: ${esc(r.target_label||String(r.target_id))}</b>
          <small>${r.status==="pending"?"⏳ новая":"✓ "+r.status} · от ${esc(r.reporter_name||"пользователя")}</small>
          <div class="report-reason">${esc(r.reason)}</div>
        </div>
        ${r.status==="pending"?`<div class="admin-actions">
          ${r.target_type==="project"&&r.target_state==="active"?`<button class="danger" onclick="moderateProject(${r.target_id},'removed')">Снять</button>`:""}
          ${r.target_type==="user"&&r.target_state==="active"?`<button class="danger" onclick="adminBlockUser(${r.target_id},true)">Блок</button>`:""}
          <button class="secondary" onclick="decideReport(${r.id},'resolved')">Готово</button>
          <button class="secondary" onclick="decideReport(${r.id},'dismissed')">Отклонить</button>
        </div>`:""}
      </div>`).join(""):'<div class="empty">Жалоб пока нет.</div>';
    $("adminProjects").innerHTML=projects.length?projects.map(p=>`
      <div class="admin-row">
        <div class="grow"><b>${esc(p.title)}</b><small>${esc(p.owner_name||"")} · ${modeLabels[p.mode]} · ${p.moderation_status}</small></div>
        <button class="${p.moderation_status==="active"?"danger":"secondary"}" onclick="moderateProject(${p.id},'${p.moderation_status==="active"?"removed":"active"}')">${p.moderation_status==="active"?"Снять":"Вернуть"}</button>
      </div>`).join(""):'<div class="empty">Проектов нет.</div>';
    $("adminUsers").innerHTML=users.length?users.map(u=>`
      <div class="admin-row">
        <div class="grow"><b>${esc(u.first_name||"Пользователь")}</b><small>${u.username?"@"+esc(u.username):"без username"} · ${esc(u.role||"роль не указана")}</small></div>
        <button class="${u.blocked?"secondary":"danger"}" onclick="adminBlockUser(${u.telegram_id},${u.blocked?"false":"true"})">${u.blocked?"Разбан":"Блок"}</button>
      </div>`).join(""):'<div class="empty">Пользователей нет.</div>';
    modal("adminModal",true);
  }catch(e){toast(e.message,"error")}
}
window.moderateProject=async(id,status)=>{try{await api(`/api/admin/projects/${id}/moderation?status=${status}`,{method:"POST"});toast(status==="removed"?"Проект снят":"Проект возвращён");loadAdmin()}catch(e){toast(e.message,"error")}};
window.decideReport=async(id,status)=>{try{await api(`/api/admin/reports/${id}/decision?status=${status}`,{method:"POST"});toast(status==="resolved"?"Жалоба закрыта":"Жалоба отклонена");loadAdmin()}catch(e){toast(e.message,"error")}};
$("cleanupDemoBtn")?.addEventListener("click",async()=>{if(!confirm("Удалить только тестовых Артёма, Леру, Мишу и их демо-проекты? Твои реальные данные останутся."))return;try{const r=await api("/api/admin/cleanup-demo",{method:"POST"});toast(`Удалено: ${r.deleted_users} людей, ${r.deleted_projects} проектов`);loadAdmin();loadFeed()}catch(e){toast(e.message,"error")}});

window.adminBlockUser=async(id,blocked)=>{try{await api(`/api/admin/users/${id}/block?blocked=${blocked}`,{method:"POST"});toast(blocked?"Пользователь заблокирован":"Пользователь разблокирован");loadAdmin()}catch(e){toast(e.message,"error")}};
$("adminBtn")?.addEventListener("click",loadAdmin);

async function bootContent(){
  await loadMe();
  renderStacks();

  if(!profileReady()){
    state.projectScope="all";
    state.peopleScope="all";
    setTab("projects");
    showFirstVisitRegistration();
    return;
  }

  state.projectScope="recommended";
  state.peopleScope="recommended";
  document.querySelectorAll("[data-scope]").forEach(b=>b.classList.toggle("active",b.dataset.scope===state.projectScope));
  document.querySelectorAll("[data-people-scope]").forEach(b=>b.classList.toggle("active",b.dataset.peopleScope===state.peopleScope));
  document.querySelectorAll("[data-mode]").forEach(b=>b.classList.toggle("active",b.dataset.mode===state.mode));

  const target=state.me?.goal==="people"?"people":"projects";
  setTab(target);
  await openDeepLink();
  maybeShowChannelBanner();
}
async function boot(){await loadConfig();if(await checkAccess())await bootContent()}
boot();

document.querySelectorAll("[data-people-scope]").forEach(b=>b.onclick=()=>{state.peopleScope=b.dataset.peopleScope;document.querySelectorAll("[data-people-scope]").forEach(x=>x.classList.toggle("active",x===b));loadFeed()});
