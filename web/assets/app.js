const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const state = {me: null, members: [], tasks: [], view: "mine", myFilter: "all", familyFilter: "all", memberFilter: "all", selectedProfile: null};
const previewMode = new URLSearchParams(location.search).has("preview");
const escapeHtml = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const initials = (name) => name.split(/\s+/).slice(0, 2).map(x => x[0]).join("").toUpperCase();

async function api(path, options = {}) {
  const response = await fetch(path, {credentials: "same-origin", ...options, headers: {"Content-Type": "application/json", ...(options.headers || {})}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Something went wrong. Please try again.");
  return data;
}
function showScreen(id) {
  ["loading", "home-screen", "setup-screen", "login-screen", "pin-setup-screen", "dashboard"].forEach(name => document.getElementById(name).classList.toggle("hidden", name !== id));
}
function showToast(message) {
  const toast = $("#toast"); toast.textContent = message; toast.classList.add("visible");
  clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toast.classList.remove("visible"), 2800);
}
function profileCard(member) {
  return `<button class="profile-option" type="button" data-member="${escapeHtml(member.id)}"><span class="avatar avatar-large">${escapeHtml(initials(member.name))}</span><span class="profile-option-name">${escapeHtml(member.name)}${member.activated ? "" : '<small>PIN setup needed</small>'}</span><span class="profile-arrow">→</span></button>`;
}
function renderSetupProfiles() {
  const host = $("#setup-members");
  const index=host.children.length;
  host.insertAdjacentHTML("beforeend", `<div class="profile-entry"><div class="profile-entry-head"><span class="entry-number">${index + 1}</span><strong>${index===0?"You · organizer":"Family member"}</strong>${index ? '<button class="remove-profile" type="button" aria-label="Remove member">×</button>' : ""}</div><label>Name<input name="member_name" required maxlength="60" placeholder="e.g. Sam"></label><label>Email <span class="optional">Optional · for email alerts later</span><input name="member_email" type="email" maxlength="254" placeholder="sam@example.com"></label></div>`);
  const last = host.lastElementChild; $(".remove-profile", last)?.addEventListener("click", () => last.remove());
}
async function start() {
  const inviteToken=new URLSearchParams(location.search).get("invite");
  if(inviteToken){
    try{const invitation=await api(`/api/invitations/${encodeURIComponent(inviteToken)}`);$("#invite-token").value=inviteToken;$("#invite-member-name").textContent=invitation.name;showScreen("pin-setup-screen");return;}
    catch(error){showScreen("home-screen");showToast(error.message);return;}
  }
  if (previewMode) {
    const people = [
      {id:"sam",name:"Sam Sharma",email:"sam@example.com",role:"admin",activated:true},
      {id:"asha",name:"Asha Sharma",email:"asha@example.com",role:"member",activated:true},
      {id:"raj",name:"Raj Sharma",email:"raj@example.com",role:"member",activated:true},
    ];
    state.tasks = [
      {id:"t1",title:"Pick up the documents from the bank",assignee_id:"sam",original_assignee_id:"sam",assignee:people[0],due_date:new Date(Date.now()+86400000).toISOString().slice(0,10),priority:"high",note:"Ask for the stamped copy at the counter.",status:"todo"},
      {id:"t2",title:"Pay the electricity bill",assignee_id:"sam",assignee:people[0],due_date:new Date().toISOString().slice(0,10),priority:"normal",note:"",status:"in_progress"},
      {id:"t3",title:"Pick up fresh vegetables",assignee_id:"asha",assignee:people[1],due_date:new Date().toISOString().slice(0,10),priority:"normal",note:"Tomatoes, spinach and potatoes.",status:"todo"},
      {id:"t4",title:"Book the plumber for Saturday",assignee_id:"raj",assignee:people[2],due_date:new Date(Date.now()+2*86400000).toISOString().slice(0,10),priority:"normal",note:"",status:"todo"},
      {id:"t5",title:"Water the balcony plants",assignee_id:"sam",assignee:people[0],due_date:new Date().toISOString().slice(0,10),priority:"low",note:"",status:"done"},
    ];
    state.tasks.forEach(task=>{task.created_by="sam";task.creator={id:"sam",name:people[0].name};});
    openDashboard({member:people[0],members:people,household:{name:"The Sharma Home"}});
    return;
  }
  try { const data = await api("/api/me"); openDashboard(data); }
  catch {
    try { const home = await api("/api/homes/current/profiles"); await renderHomeProfiles(home); }
    catch { showScreen("home-screen"); }
  }
}
async function renderHomeProfiles(data) {
  $("#home-profiles-wrap").classList.remove("hidden"); $("#home-login-form").classList.add("hidden"); $("#toggle-create-home").classList.add("hidden"); $("#create-home-form").classList.add("hidden");
  $("#home-profile-login").classList.add("hidden"); $("#home-profile-login").reset();
  $("#home-profile-list").innerHTML=data.members.map(profileCard).join("");
  $$(".profile-option",$("#home-profile-list")).forEach(button=>button.addEventListener("click",()=>{
    const selected=data.members.find(member=>member.id===button.dataset.member);
    if(!selected.activated){$("#home-profile-error").textContent="This profile is waiting for its private setup link from the organizer.";return;}
    $("#home-profile-error").textContent="";$("#home-profile-login [name=member_id]").value=selected.id;$("#home-profile-pin-label").firstChild.textContent=selected.name+"’s profile PIN";
    $("#home-profile-login").classList.remove("hidden");$("#home-profile-login [name=pin]").value="";$("#home-profile-login [name=pin]").focus();
    $$(".profile-option",$("#home-profile-list")).forEach(item=>item.classList.toggle("selected",item===button));
  }));
}
$("#home-login-form").addEventListener("submit",async e=>{
  e.preventDefault();const f=new FormData(e.currentTarget);const error=$("#home-login-error");error.textContent="";
  try{await api("/api/homes/login",{method:"POST",body:JSON.stringify({username:f.get("username"),pin:f.get("pin")})});const data=await api("/api/homes/current/profiles");await renderHomeProfiles(data);}
  catch(err){error.textContent=err.message;}
});
$("#home-credentials-button").addEventListener("click",()=>{$("#home-credentials-error").textContent="";$("#home-credentials-dialog").showModal();});
$("#close-home-credentials").addEventListener("click",()=>$("#home-credentials-dialog").close());
$("#home-credentials-dialog").addEventListener("click",e=>{if(e.target===$("#home-credentials-dialog"))$("#home-credentials-dialog").close();});
$("#home-credentials-form").addEventListener("submit",async e=>{
  e.preventDefault();const f=new FormData(e.currentTarget);const error=$("#home-credentials-error");error.textContent="";
  if(f.get("pin")!==f.get("confirm_pin")){error.textContent="The home PINs don’t match.";return;}
  try{await api("/api/homes/credentials",{method:"POST",body:JSON.stringify({username:f.get("username"),pin:f.get("pin")})});$("#home-credentials-dialog").close();e.currentTarget.reset();showToast("Home sign-in saved. Keep the username and PIN safe.");}
  catch(err){error.textContent=err.message;}
});
$("#toggle-create-home").addEventListener("click",()=>{$("#create-home-form").classList.toggle("hidden");$("#home-login-error").textContent="";});
$("#create-home-form").addEventListener("submit",async e=>{
  e.preventDefault();const f=new FormData(e.currentTarget);const error=$("#create-home-error");error.textContent="";
  if(f.get("pin")!==f.get("confirm_home_pin")){error.textContent="The home PINs don’t match.";return;}
  if(f.get("organizer_pin")!==f.get("confirm_organizer_pin")){error.textContent="Your profile PINs don’t match.";return;}
  try{await api("/api/homes",{method:"POST",body:JSON.stringify(Object.fromEntries(f))});const data=await api("/api/homes/current/profiles");await renderHomeProfiles(data);showToast("Home created. Sign in with your organizer profile PIN.");}
  catch(err){error.textContent=err.message;}
});
$("#home-profile-login").addEventListener("submit",async e=>{
  e.preventDefault();const f=new FormData(e.currentTarget);const error=$("#home-profile-error");error.textContent="";
  try{await api("/api/login",{method:"POST",body:JSON.stringify({member_id:f.get("member_id"),pin:f.get("pin")})});openDashboard(await api("/api/me"));}
  catch(err){error.textContent=err.message;$("#home-profile-login [name=pin]").value="";}
});
$("#change-pin-button").addEventListener("click",()=>{$("#change-pin-error").textContent="";$("#change-pin-dialog").showModal();});
$("#close-change-pin").addEventListener("click",()=>$("#change-pin-dialog").close());
$("#change-pin-dialog").addEventListener("click",e=>{if(e.target===$("#change-pin-dialog"))$("#change-pin-dialog").close();});
$("#change-pin-form").addEventListener("submit",async e=>{
  e.preventDefault();const f=new FormData(e.currentTarget);const error=$("#change-pin-error");error.textContent="";
  if(f.get("new_pin")!==f.get("confirm_pin")){error.textContent="The new PINs don’t match.";return;}
  try{await api("/api/me/pin",{method:"POST",body:JSON.stringify({current_pin:f.get("current_pin"),new_pin:f.get("new_pin")})});$("#change-pin-dialog").close();e.currentTarget.reset();showToast("Your profile PIN has been changed.");}
  catch(err){error.textContent=err.message;}
});
$("#activation-form").addEventListener("submit", async e => {
  e.preventDefault(); const form=e.currentTarget; const pin=$("#new-pin").value; const confirm=$("#confirm-pin").value; const error=$("#activation-error"); error.textContent="";
  if(pin!==confirm){error.textContent="Those PINs don’t match. Please enter them again.";return;}
  const button=$("[type=submit]",form); button.disabled=true;
  try{await api("/api/invitations/activate",{method:"POST",body:JSON.stringify({token:$("#invite-token").value,pin})});history.replaceState({},"",location.pathname);openDashboard(await api("/api/me"));showToast("Your PIN is ready. Welcome to the family space!");}
  catch(err){error.textContent=err.message;}finally{button.disabled=false;}
});
async function showLogin() {
  const status = await api("/api/setup/status");
  if (!status.configured) { renderSetupProfiles(); showScreen("setup-screen"); return; }
  const members = await api("/api/members/public").catch(() => null);
  if (members) state.members = members.members;
  else { showToast("Could not load family profiles."); return; }
  $("#profile-list").innerHTML = state.members.map(profileCard).join("");
  $$(".profile-option").forEach(button => button.addEventListener("click", () => {
    state.selectedProfile = button.dataset.member;
    const selected=state.members.find(m=>m.id===state.selectedProfile);
    if(!selected.activated){
      $("#login-form").classList.add("hidden");
      $("#login-pending").textContent=selected.invited?"Your setup link is ready. Ask the organizer to share it with you.":"Your profile is ready. Ask the household organizer for a secure setup link.";
      $("#login-pending").classList.remove("hidden");
      $$(".profile-option").forEach(b=>b.classList.toggle("selected",b===button));
      return;
    }
    $("#login-pending").classList.add("hidden");
    $("#login-form [name=member_id]").value = state.selectedProfile;
    $("#pin-label").textContent = `${selected.name}’s PIN`;
    $("#login-form").classList.remove("hidden"); $("#login-pin").value = ""; $("#login-pin").focus();
    $$(".profile-option").forEach(b => b.classList.toggle("selected", b === button));
  }));
  showScreen("login-screen");
}
function openDashboard(data) {
  state.me = data.member; state.members = data.members || [];
  $("#preview-badge").classList.toggle("hidden", !previewMode);
  if (data.household) $("#family-title").textContent = `${data.household.name}’s family board`;
  $("#account-name").textContent = state.me.name.split(" ")[0]; $("#account-avatar").textContent = initials(state.me.name);
  const hour = new Date().getHours(); const greeting = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  $("#welcome-title").textContent = `${greeting}, ${state.me.name.split(" ")[0]}`;
  const dateText = new Intl.DateTimeFormat(undefined, {weekday:"long", month:"long", day:"numeric"}).format(new Date());
  $("#today-label").textContent = dateText.toUpperCase();
  populateMembers(); showScreen("dashboard"); if(previewMode) renderTasks(); else loadTasks();
}
async function loadTasks() {
  try { const data = await api("/api/tasks"); state.tasks = data.tasks; renderTasks(); }
  catch (e) { showToast(e.message); }
}
function populateMembers() {
  $("#assignee-select").innerHTML = state.members.map(m => `<option value="${escapeHtml(m.id)}" ${m.id === state.me.id ? "selected" : ""}>${escapeHtml(m.name)}</option>`).join("");
  $("#member-filter").innerHTML = `<option value="all">Everyone</option>` + state.members.map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join("");
  $("#manage-members").classList.toggle("hidden", state.me.role !== "admin");
  $("#home-credentials-button").classList.toggle("hidden", state.me.role !== "admin" || previewMode);
  renderMemberNotices();
}
function renderMemberNotices(){
  const host=$("#member-notices");const pending=state.members.filter(m=>!m.activated);
  if(state.me.role!=="admin"||!pending.length){host.classList.add("hidden");host.innerHTML="";return;}
  host.classList.remove("hidden");
  host.innerHTML=`<div class="member-notice-heading"><div><strong>PIN setup pending</strong><span>${pending.length} ${pending.length===1?"person needs":"people need"} to activate their profile</span></div></div><div class="pending-members">${pending.map(m=>`<div class="pending-member"><span class="avatar avatar-xs">${escapeHtml(initials(m.name))}</span><span>${escapeHtml(m.name)}</span><button class="send-invite" data-member="${escapeHtml(m.id)}" type="button">${m.invited?"Get new setup link":"Get setup link"}</button></div>`).join("")}</div>`;
  $$(".send-invite",host).forEach(button=>button.addEventListener("click",async()=>{
    if(previewMode){showToast("Sample preview only");return;}
    button.disabled=true;
    try{const result=await api(`/api/members/${button.dataset.member}/invite`,{method:"POST"});const index=state.members.findIndex(m=>m.id===result.member.id);state.members[index]=result.member;populateMembers();openInviteLink(result.invite_url,result.member.name,result.invitation_sent);}
    catch(error){showToast(error.message);}finally{button.disabled=false;}
  }));
}
async function openInviteLink(url,name,emailSent=false){
  $("#invite-link-member").textContent=name;
  $("#invite-url").value=url;
  $("#invite-link-dialog").showModal();
  try{await navigator.clipboard.writeText(url);showToast(emailSent?"Setup email sent · link copied":"Link copied · share it privately by WhatsApp or text");}
  catch{const input=$("#invite-url");input.focus();input.select();showToast("Setup link ready · copy it and share privately");}
}
function dueLabel(task) {
  if (!task.due_date) return "No due date";
  const due = new Date(`${task.due_date}T12:00:00`); const today = new Date(); today.setHours(0,0,0,0); due.setHours(0,0,0,0);
  const diff = Math.round((due - today) / 86400000);
  if (task.status === "done") return `Due ${new Intl.DateTimeFormat(undefined,{month:"short",day:"numeric"}).format(due)}`;
  if (diff < 0) return `Overdue by ${Math.abs(diff)} ${Math.abs(diff) === 1 ? "day" : "days"}`;
  if (diff === 0) return "Due today";
  if (diff === 1) return "Due tomorrow";
  return `Due ${new Intl.DateTimeFormat(undefined,{month:"short",day:"numeric"}).format(due)}`;
}
function taskCard(task, family = false) {
  const statusText = {todo:"To do",in_progress:"In progress",done:"Done"}[task.status];
  const nextStatus = {todo:"in_progress",in_progress:"done",done:"todo"}[task.status];
  const actionText = {todo:"Start task",in_progress:"Mark done",done:"Reopen task"}[task.status];
  const overdue = task.status !== "done" && task.due_date && new Date(`${task.due_date}T12:00:00`) < new Date(new Date().setHours(0,0,0,0));
  return `<article class="task-card ${task.status === "done" ? "is-done" : ""}"><button class="task-check ${task.status === "done" ? "checked" : ""}" data-task="${escapeHtml(task.id)}" data-next="${nextStatus}" aria-label="${actionText}" title="${actionText}" type="button">${task.status === "done" ? "✓" : task.status === "in_progress" ? "◷" : ""}</button><div class="task-main"><div class="task-title-row"><button class="task-title-button" data-task-detail="${escapeHtml(task.id)}" type="button"><h3>${escapeHtml(task.title)}</h3></button>${task.priority === "high" ? '<span class="priority-badge">Important</span>' : ""}</div>${task.note ? `<p class="task-note">${escapeHtml(task.note)}</p>` : ""}<div class="task-meta"><span class="due-pill ${overdue ? "overdue" : ""}"><span>◷</span>${dueLabel(task)}</span>${family ? `<span class="assignee"><span class="avatar avatar-xs">${escapeHtml(initials(task.assignee?.name || "?"))}</span>${escapeHtml(task.assignee?.name || "Unassigned")}</span>` : ""}<button class="task-details-trigger" data-task-detail="${escapeHtml(task.id)}" type="button">Details</button></div></div><span class="status-pill status-${task.status}">${statusText}</span></article>`;
}
function renderTasks() {
  const mine = state.tasks.filter(t => t.assignee_id === state.me.id);
  const done = mine.filter(t => t.status === "done").length;
  const open = mine.filter(t => t.status !== "done").length;
  const pct = mine.length ? Math.round(done / mine.length * 100) : 0;
  $("#my-count").textContent = open;
  $("#hero-progress").style.width = `${pct}%`; $("#hero-percent").textContent = `${pct}%`;
  $("#hero-progress-text").textContent = `${done} of ${mine.length} ${mine.length === 1 ? "task" : "tasks"} done`;
  $("#hero-title").textContent = mine.length && done === mine.length ? "You did it!" : open === 0 ? "A fresh start." : open === 1 ? "One thing at a time." : "You’ve got this.";
  $("#hero-copy").textContent = mine.length && done === mine.length ? "Everything on your list is taken care of." : open ? `${open} ${open === 1 ? "task is" : "tasks are"} waiting for you.` : "Small things add up. You’ve got this.";
  const myTasks = mine.filter(t => state.myFilter === "all" || (state.myFilter === "active" ? t.status !== "done" : t.status === state.myFilter));
  $("#my-task-list").innerHTML = myTasks.map(t => taskCard(t)).join(""); $("#my-empty").classList.toggle("hidden", myTasks.length !== 0);
  const filtered = state.tasks.filter(t => (state.familyFilter === "all" || t.status === state.familyFilter) && (state.memberFilter === "all" || t.assignee_id === state.memberFilter));
  $("#family-task-list").innerHTML = filtered.map(t => taskCard(t, true)).join(""); $("#family-empty").classList.toggle("hidden", filtered.length !== 0);
  const allDone = state.tasks.filter(t => t.status === "done").length;
  $("#family-progress-number").textContent = `${state.tasks.length ? Math.round(allDone / state.tasks.length * 100) : 0}%`;
  $("#family-stats").innerHTML = [{key:"todo",label:"To do",icon:"○"},{key:"in_progress",label:"In progress",icon:"◷"},{key:"done",label:"Done",icon:"✓"}].map(s => `<div class="stat-card stat-${s.key}"><span class="stat-icon">${s.icon}</span><span class="stat-value">${state.tasks.filter(t => t.status === s.key).length}</span><span class="stat-label">${s.label}</span></div>`).join("");
  $$(".task-check").forEach(button => button.addEventListener("click", () => updateStatus(button.dataset.task, button.dataset.next)));
  $$("[data-task-detail]").forEach(button => button.addEventListener("click", () => openTaskDetails(button.dataset.taskDetail)));
}
function showTaskDetailMode(editing){
  $("#task-details-view").classList.toggle("hidden",editing);
  $("#task-edit-form").classList.toggle("hidden",!editing);
  $("#task-delete-button").classList.toggle("hidden",editing);
  $("#task-detail-edit").classList.toggle("hidden",editing);
  $("#task-detail-heading").textContent=editing?"Edit task":"Task details";
}
function openTaskDetails(taskId){
  const task=state.tasks.find(item=>item.id===taskId);if(!task)return;
  state.activeTaskId=taskId;
  $("#detail-task-title").textContent=task.title;
  $("#detail-task-assignee").textContent=task.assignee?.name||state.members.find(m=>m.id===task.assignee_id)?.name||"Unassigned";
  $("#detail-task-due").textContent=task.due_date?new Intl.DateTimeFormat(undefined,{year:"numeric",month:"long",day:"numeric"}).format(new Date(`${task.due_date}T12:00:00`)):"No due date";
  $("#detail-task-priority").textContent={low:"Whenever",normal:"Normal",high:"Important"}[task.priority]||"Normal";
  $("#detail-task-status").textContent={todo:"To do",in_progress:"In progress",done:"Done"}[task.status]||task.status;
  $("#detail-task-note").textContent=task.note||"No description added.";
  $("#detail-task-creator").textContent=task.creator?.name||state.members.find(m=>m.id===task.created_by)?.name||"Unknown";
  $("#detail-task-created").textContent=task.created_at?new Intl.DateTimeFormat(undefined,{year:"numeric",month:"long",day:"numeric"}).format(new Date(task.created_at)):"—";
  const isCreator=task.created_by===state.me.id;
  $("#task-owner-actions").classList.toggle("hidden",!isCreator);
  $("#edit-task-title").value=task.title;
  const editAssignee=task.status==="done"?(task.original_assignee_id||task.assignee_id):task.assignee_id;
  $("#edit-task-assignee-label").textContent=task.status==="done"?"Assigned to when reopened":"Assigned to";
  $("#edit-task-assignee").innerHTML=state.members.map(m=>`<option value="${escapeHtml(m.id)}" ${m.id===editAssignee?"selected":""}>${escapeHtml(m.name)}</option>`).join("");
  $("#edit-task-due").value=task.due_date||"";
  $("#edit-task-note").value=task.note||"";
  $$("#task-edit-form [name=priority]").forEach(input=>input.checked=input.value===task.priority);
  showTaskDetailMode(false);
  $("#task-detail-dialog").showModal();
}
async function updateStatus(id, status) {
  if (previewMode) {
    const task=state.tasks.find(t=>t.id===id);
    if(status==="done"&&task.status!=="done"){task.original_assignee_id??=task.assignee_id;task.assignee_id=state.me.id;task.completed_by=state.me.id;}
    else if(task.status==="done"&&status!=="done"){task.assignee_id=task.original_assignee_id??task.assignee_id;task.completed_by=null;}
    task.status=status;task.assignee=state.members.find(m=>m.id===task.assignee_id);renderTasks();showToast(status==="done"?`Completed · now assigned to ${state.me.name.split(" ")[0]}`:"Reopened · returned to the original assignee");return;
  }
  try { await api(`/api/tasks/${id}`, {method:"PATCH", body:JSON.stringify({status})}); await loadTasks(); showToast(status==="done"?`Completed · now assigned to ${state.me.name.split(" ")[0]}`:"Reopened · returned to the original assignee"); }
  catch (e) { showToast(e.message); }
}
function setView(view) {
  state.view = view; $("#my-home").classList.toggle("hidden", view !== "mine"); $("#family-home").classList.toggle("hidden", view !== "family");
  $$(".view-tab,.mobile-nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === view));
}
function openTaskDialog() { $("#task-dialog").showModal(); $("#task-form [name=title]").focus(); }

$("#add-profile").addEventListener("click", () => renderSetupProfiles());
$("#setup-form").addEventListener("submit", async e => {
  e.preventDefault(); const form = e.currentTarget; const entries = $$(".profile-entry", form);
  const members = entries.map(entry => ({name:$("[name=member_name]",entry).value, email:$("[name=member_email]",entry).value}));
  const submit = $("[type=submit]",form); submit.disabled = true; submit.textContent = "Setting up your home…";
  try { const data = await api("/api/setup", {method:"POST",body:JSON.stringify({household_name:$('[name=household_name]',form).value,members,organizer_pin:$("#organizer-pin").value})}); const me = await api("/api/me"); openDashboard(me); showToast(data.invites_pending?`Welcome! Get setup links for ${data.invites_pending} family member(s) on the Family board.`:`Welcome to ${data.household.name}!`); }
  catch(e) { showToast(e.message); submit.disabled = false; submit.innerHTML = 'Create our family space <span>→</span>'; }
});
$("#login-form").addEventListener("submit", async e => {
  e.preventDefault(); const form = e.currentTarget; const err = $("#login-error"); err.textContent = "";
  try { await api("/api/login", {method:"POST",body:JSON.stringify({member_id:$("[name=member_id]",form).value,pin:$("[name=pin]",form).value})}); openDashboard(await api("/api/me")); }
  catch(error) { err.textContent = error.message; $("#login-pin").value = ""; $("#login-pin").focus(); }
});
function closeAccountMenu(){$("#account-menu").classList.add("hidden");$("#account-button").setAttribute("aria-expanded","false");}
$("#account-button").addEventListener("click",()=>{const menu=$("#account-menu");const opening=menu.classList.contains("hidden");menu.classList.toggle("hidden",!opening);$("#account-button").setAttribute("aria-expanded",String(opening));});
document.addEventListener("click",e=>{if(!e.target.closest(".account-menu-wrap"))closeAccountMenu();});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeAccountMenu();});
$("#profile-signout").addEventListener("click",async()=>{
  closeAccountMenu();
  if(previewMode){showToast("You’re viewing a sample family space");return;}
  try{await api("/api/logout",{method:"POST"});state.me=null;const data=await api("/api/homes/current/profiles");await renderHomeProfiles(data);showScreen("home-screen");}
  catch(error){showScreen("home-screen");showToast(error.message);}
});
$("#sign-out-home").addEventListener("click",async()=>{
  try{await api("/api/homes/logout",{method:"POST"});$("#home-profiles-wrap").classList.add("hidden");$("#home-login-form").classList.remove("hidden");$("#toggle-create-home").classList.remove("hidden");$("#create-home-form").classList.add("hidden");$("#home-login-form").reset();$("#home-login-error").textContent="";showScreen("home-screen");}
  catch(error){showToast(error.message);}
});
$("#task-form").addEventListener("submit", async e => {
  e.preventDefault(); const form=e.currentTarget; const f=new FormData(form); const payload={title:f.get("title"),assignee_id:f.get("assignee_id"),due_date:f.get("due_date")||null,priority:f.get("priority"),note:f.get("note")};
  try { if(previewMode){const assignee=state.members.find(m=>m.id===payload.assignee_id);state.tasks.unshift({id:crypto.randomUUID(),...payload,status:"todo",assignee,created_by:state.me.id,creator:{id:state.me.id,name:state.me.name},original_assignee_id:payload.assignee_id});renderTasks();$("#task-dialog").close();form.reset();showToast("Task added to the family list");return;} const result=await api("/api/tasks",{method:"POST",body:JSON.stringify(payload)}); $("#task-dialog").close(); form.reset(); $("#task-form [name=priority][value=normal]").checked=true; await loadTasks(); showToast(result.email_sent?"Task added · assignment email sent":"Task added · email alerts need setup"); }
  catch(error) { showToast(error.message); }
});
$("#close-dialog").addEventListener("click",()=>$("#task-dialog").close());
$("#task-dialog").addEventListener("click", e=>{if(e.target === $("#task-dialog")) $("#task-dialog").close();});
$("#task-detail-edit").addEventListener("click",()=>showTaskDetailMode(true));
$("#task-detail-cancel").addEventListener("click",()=>showTaskDetailMode(false));
$("#close-task-detail").addEventListener("click",()=>$("#task-detail-dialog").close());
$("#task-detail-dialog").addEventListener("click",e=>{if(e.target===$("#task-detail-dialog")) $("#task-detail-dialog").close();});
$("#task-edit-form").addEventListener("submit",async e=>{
  e.preventDefault();
  const task=state.tasks.find(item=>item.id===state.activeTaskId);if(!task||task.created_by!==state.me.id){showToast("Only the task creator can edit this task");return;}
  const f=new FormData(e.currentTarget);const payload={title:f.get("title"),assignee_id:f.get("assignee_id"),due_date:f.get("due_date")||null,priority:f.get("priority"),note:f.get("note")};
  const button=$("[type=submit]",e.currentTarget);button.disabled=true;
  try{
    if(previewMode){const wasDone=task.status==="done",currentAssignee=task.assignee_id,completedBy=task.completed_by;Object.assign(task,payload,{original_assignee_id:payload.assignee_id});if(wasDone)task.assignee_id=completedBy||currentAssignee;task.assignee=state.members.find(m=>m.id===task.assignee_id);renderTasks();}
    else{await api(`/api/tasks/${task.id}/details`,{method:"PATCH",body:JSON.stringify(payload)});await loadTasks();}
    $("#task-detail-dialog").close();showToast("Task details updated");
  }catch(error){showToast(error.message);}finally{button.disabled=false;}
});
$("#task-delete-button").addEventListener("click",async()=>{
  const task=state.tasks.find(item=>item.id===state.activeTaskId);if(!task||task.created_by!==state.me.id){showToast("Only the task creator can delete this task");return;}
  if(!window.confirm(`Delete “${task.title}”? This can’t be undone.`))return;
  try{if(previewMode){state.tasks=state.tasks.filter(item=>item.id!==task.id);renderTasks();}else{await api(`/api/tasks/${task.id}`,{method:"DELETE"});await loadTasks();}$("#task-detail-dialog").close();showToast("Task deleted");}
  catch(error){showToast(error.message);}
});
$("#manage-members").addEventListener("click",()=>$("#member-dialog").showModal());
$("#close-member-dialog").addEventListener("click",()=>$("#member-dialog").close());
$("#member-dialog").addEventListener("click",e=>{if(e.target===$("#member-dialog")) $("#member-dialog").close();});
$("#close-invite-link").addEventListener("click",()=>$("#invite-link-dialog").close());
$("#invite-link-dialog").addEventListener("click",e=>{if(e.target===$("#invite-link-dialog")) $("#invite-link-dialog").close();});
$("#copy-invite-link").addEventListener("click",async()=>{const input=$("#invite-url");try{await navigator.clipboard.writeText(input.value);showToast("Link copied · share it privately");}catch{input.focus();input.select();document.execCommand("copy");showToast("Link copied · share it privately");}});
$("#member-form").addEventListener("submit",async e=>{
  e.preventDefault(); const f=new FormData(e.currentTarget); const button=$("[type=submit]",e.currentTarget); button.disabled=true;
  try { const data=previewMode?{member:{id:crypto.randomUUID(),name:f.get("name"),email:f.get("email"),role:"member",activated:true}}:await api("/api/members",{method:"POST",body:JSON.stringify({name:f.get("name"),email:f.get("email")})}); state.members.push(data.member); populateMembers(); $("#member-dialog").close(); e.currentTarget.reset(); showToast(`${data.member.name} added · get their setup link on the Family board`); }
  catch(error){showToast(error.message);} finally{button.disabled=false;}
});
["#add-task-top","#mobile-add","#empty-add","#family-empty-add"].forEach(id=>$(id).addEventListener("click",openTaskDialog));
$$('[data-view]').forEach(button=>button.addEventListener("click",()=>setView(button.dataset.view)));
$$('[data-filter]').forEach(button=>button.addEventListener("click",()=>{state.familyFilter=button.dataset.filter; $$('.filter-chip').forEach(b=>b.classList.toggle("selected",b===button)); renderTasks();}));
$("#member-filter").addEventListener("change",e=>{state.memberFilter=e.target.value;renderTasks();});
$("#my-filter").addEventListener("click",()=>{state.myFilter=state.myFilter==="active"?"all":"active";$("#my-filter").innerHTML=`${state.myFilter==="active"?"Active":"All"} <span>⌄</span>`;renderTasks();});
const savedTheme=localStorage.getItem("together-theme"); if(savedTheme) document.documentElement.dataset.theme=savedTheme;
function updateThemeIcon(){ $("#theme-icon").textContent=document.documentElement.dataset.theme==="dark"?"☀":"☾"; }
updateThemeIcon();
$("#theme-toggle").addEventListener("click",()=>{const next=document.documentElement.dataset.theme==="dark"?"light":"dark";document.documentElement.dataset.theme=next;localStorage.setItem("together-theme",next);updateThemeIcon();});
start();
