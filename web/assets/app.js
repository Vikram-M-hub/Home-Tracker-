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
  ["loading", "setup-screen", "login-screen", "pin-setup-screen", "dashboard"].forEach(name => $(`#${name}`).classList.toggle("hidden", name !== id));
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
  host.insertAdjacentHTML("beforeend", `<div class="profile-entry"><div class="profile-entry-head"><span class="entry-number">${index + 1}</span><strong>${index===0?"You · organizer":"Family member"}</strong>${index ? '<button class="remove-profile" type="button" aria-label="Remove member">×</button>' : ""}</div><label>Name<input name="member_name" required maxlength="60" placeholder="e.g. Sam"></label><label>Email<input name="member_email" type="email" required maxlength="254" placeholder="sam@example.com"></label></div>`);
  const last = host.lastElementChild; $(".remove-profile", last)?.addEventListener("click", () => last.remove());
}
async function start() {
  const inviteToken=new URLSearchParams(location.search).get("invite");
  if(inviteToken){
    try{const invitation=await api(`/api/invitations/${encodeURIComponent(inviteToken)}`);$("#invite-token").value=inviteToken;$("#invite-member-name").textContent=invitation.name;showScreen("pin-setup-screen");return;}
    catch(error){await showLogin().catch(()=>showScreen("login-screen"));showToast(error.message);return;}
  }
  if (previewMode) {
    const people = [
      {id:"sam",name:"Sam Sharma",email:"sam@example.com",role:"admin",activated:true},
      {id:"asha",name:"Asha Sharma",email:"asha@example.com",role:"member",activated:true},
      {id:"raj",name:"Raj Sharma",email:"raj@example.com",role:"member",activated:true},
    ];
    state.tasks = [
      {id:"t1",title:"Pick up the documents from the bank",assignee_id:"sam",assignee:people[0],due_date:new Date(Date.now()+86400000).toISOString().slice(0,10),priority:"high",note:"Ask for the stamped copy at the counter.",status:"todo"},
      {id:"t2",title:"Pay the electricity bill",assignee_id:"sam",assignee:people[0],due_date:new Date().toISOString().slice(0,10),priority:"normal",note:"",status:"in_progress"},
      {id:"t3",title:"Pick up fresh vegetables",assignee_id:"asha",assignee:people[1],due_date:new Date().toISOString().slice(0,10),priority:"normal",note:"Tomatoes, spinach and potatoes.",status:"todo"},
      {id:"t4",title:"Book the plumber for Saturday",assignee_id:"raj",assignee:people[2],due_date:new Date(Date.now()+2*86400000).toISOString().slice(0,10),priority:"normal",note:"",status:"todo"},
      {id:"t5",title:"Water the balcony plants",assignee_id:"sam",assignee:people[0],due_date:new Date().toISOString().slice(0,10),priority:"low",note:"",status:"done"},
    ];
    openDashboard({member:people[0],members:people,household:{name:"The Sharma Home"}});
    return;
  }
  try {
    const {configured} = await api("/api/setup/status");
    if (!configured) { renderSetupProfiles(); showScreen("setup-screen"); return; }
    try { const data = await api("/api/me"); openDashboard(data); }
    catch { await showLogin(); }
  } catch { showScreen("setup-screen"); showToast("Could not connect. Refresh to try again."); }
}
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
  return `<article class="task-card ${task.status === "done" ? "is-done" : ""}"><button class="task-check ${task.status === "done" ? "checked" : ""}" data-task="${escapeHtml(task.id)}" data-next="${nextStatus}" aria-label="${actionText}" title="${actionText}" type="button">${task.status === "done" ? "✓" : task.status === "in_progress" ? "◷" : ""}</button><div class="task-main"><div class="task-title-row"><h3>${escapeHtml(task.title)}</h3>${task.priority === "high" ? '<span class="priority-badge">Important</span>' : ""}</div>${task.note ? `<p class="task-note">${escapeHtml(task.note)}</p>` : ""}<div class="task-meta"><span class="due-pill ${overdue ? "overdue" : ""}"><span>◷</span>${dueLabel(task)}</span>${family ? `<span class="assignee"><span class="avatar avatar-xs">${escapeHtml(initials(task.assignee?.name || "?"))}</span>${escapeHtml(task.assignee?.name || "Unassigned")}</span>` : ""}</div></div><span class="status-pill status-${task.status}">${statusText}</span></article>`;
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
}
async function updateStatus(id, status) {
  if (previewMode) { state.tasks.find(t => t.id === id).status=status; renderTasks(); showToast("Task updated"); return; }
  try { await api(`/api/tasks/${id}`, {method:"PATCH", body:JSON.stringify({status})}); await loadTasks(); }
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
$("#account-button").addEventListener("click", async () => { if(previewMode){showToast("You’re viewing a sample family space");return;} await api("/api/logout", {method:"POST"}); state.me = null; await showLogin(); });
$("#task-form").addEventListener("submit", async e => {
  e.preventDefault(); const form=e.currentTarget; const f=new FormData(form); const payload={title:f.get("title"),assignee_id:f.get("assignee_id"),due_date:f.get("due_date")||null,priority:f.get("priority"),note:f.get("note")};
  try { if(previewMode){const assignee=state.members.find(m=>m.id===payload.assignee_id);state.tasks.unshift({id:crypto.randomUUID(),...payload,status:"todo",assignee});renderTasks();$("#task-dialog").close();form.reset();showToast("Task added to the family list");return;} const result=await api("/api/tasks",{method:"POST",body:JSON.stringify(payload)}); $("#task-dialog").close(); form.reset(); $("#task-form [name=priority][value=normal]").checked=true; await loadTasks(); showToast(result.email_sent?"Task added · assignment email sent":"Task added · email alerts need setup"); }
  catch(error) { showToast(error.message); }
});
$("#close-dialog").addEventListener("click",()=>$("#task-dialog").close());
$("#task-dialog").addEventListener("click", e=>{if(e.target === $("#task-dialog")) $("#task-dialog").close();});
$("#manage-members").addEventListener("click",()=>$("#member-dialog").showModal());
$("#close-member-dialog").addEventListener("click",()=>$("#member-dialog").close());
$("#member-dialog").addEventListener("click",e=>{if(e.target===$("#member-dialog")) $("#member-dialog").close();});
$("#close-invite-link").addEventListener("click",()=>$("#invite-link-dialog").close());
$("#invite-link-dialog").addEventListener("click",e=>{if(e.target===$("#invite-link-dialog")) $("#invite-link-dialog").close();});
$("#copy-invite-link").addEventListener("click",async()=>{const input=$("#invite-url");try{await navigator.clipboard.writeText(input.value);showToast("Link copied · share it privately");}catch{input.focus();input.select();document.execCommand("copy");showToast("Link copied · share it privately");}});
$("#member-form").addEventListener("submit",async e=>{
  e.preventDefault(); const f=new FormData(e.currentTarget); const button=$("[type=submit]",e.currentTarget); button.disabled=true;
  try { const data=previewMode?{member:{id:crypto.randomUUID(),name:f.get("name"),email:f.get("email"),role:"member",activated:true}}:await api("/api/members",{method:"POST",body:JSON.stringify({name:f.get("name"),email:f.get("email")})}); state.members.push(data.member); populateMembers(); $("#member-dialog").close(); e.currentTarget.reset(); showToast(data.invitation_sent===false?"Member added · email delivery still needs setup":`${data.member.name} is in the family space`); }
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
