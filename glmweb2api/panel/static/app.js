"use strict";
const $ = (id) => document.getElementById(id);
const state = { csrf: "", page: "overview", usagePage: 1, auditPage: 1, login: null, config: null, test: null, chatMode: null, chatReady: false, chatBusy: false, chatAbort: null, chatMessages: [] };
const pages = {
  overview: ["总览", "服务总览", "每一次调用，都清晰可见。"],
  chat: ["聊天", "AI 聊天", "对话与 Agent 两种后端，一个界面切换。"],
  usage: ["用量与日志", "用量与请求日志", "按时间、模型和客户端，了解你的服务。"],
  accounts: ["上游账号", "连接你的账号", "登录、刷新与接入，在这里完成。"],
  keys: ["API 密钥", "管理调用密钥", "每个客户端，都有自己的通行证。"],
  config: ["反代配置", "让服务按你的方式运行", "明确配置来源，让每一次修改都可追溯。"],
  test: ["调用测试", "试一次，便知连通", "直接验证模型的流式响应。"],
  connect: ["客户端接入", "接入你的工作流", "复制配置，开始使用你喜欢的客户端。"],
  audit: ["变更记录", "每个变更，有迹可循", "查看管理操作、配置修改与账号接入记录。"]
};
function el(tag, text, cls) { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; }
function icon(name) {
  const node = el("span", null, `fluent-icon icon-${name}`);
  node.setAttribute("aria-hidden", "true");
  return node;
}
function clear(id) { const node = typeof id === "string" ? $(id) : id; node.replaceChildren(); return node; }
function number(n) { return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(n || 0); }
function date(t) { return t ? new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }) : "—"; }
function ms(n) { return n == null ? "—" : n >= 1000 ? `${number(n / 1000)} s` : `${number(n)} ms`; }
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => $("toast").hidden = true, 4500); }
function error(message) { $("page-error").textContent = message; $("page-error").hidden = false; }
function loggedOut() { state.login = null; clearLoginImages(); loginButtons(false); $("official-login").hidden = true; $("app").hidden = true; $("login-view").hidden = false; state.csrf = ""; clearTimeout(state.loginTimer); }
async function api(path, options = {}) {
  const headers = { "X-CSRF-Token": state.csrf, ...(options.headers || {}) };
  if (options.body !== undefined) { headers["Content-Type"] = "application/json"; options.body = JSON.stringify(options.body); }
  const response = await fetch(`/panel/api${path}`, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/login") loggedOut();
    throw new Error(typeof data.detail === "string" ? data.detail : data.error?.message || `请求失败 (${response.status})`);
  }
  return data;
}
function bind(id, type, callback) {
  $(id).addEventListener(type, async (event) => {
    if (type === "submit") event.preventDefault();
    const button = type === "submit" ? event.submitter : event.currentTarget;
    if (type === "submit" && button?.tagName === "BUTTON") button.disabled = true;
    try { await callback(event); } catch (e) { error(e.message); toast(e.message); }
    finally { if (type === "submit" && button?.tagName === "BUTTON") button.disabled = false; }
  });
}
function action(label, callback, cls = "quiet") {
  const button = el("button", label, cls); button.type = "button";
  button.addEventListener("click", async () => { button.disabled = true; try { await callback(); } catch (e) { error(e.message); } finally { button.disabled = false; } });
  return button;
}
async function confirmAction(title, detail) {
  $("confirm-title").textContent = title; $("confirm-detail").textContent = detail; $("confirm-dialog").returnValue = "";
  $("confirm-dialog").showModal();
  return new Promise(resolve => $("confirm-dialog").addEventListener("close", () => resolve($("confirm-dialog").returnValue === "yes"), { once: true }));
}
$("confirm-cancel").onclick = () => $("confirm-dialog").close("no");
$("confirm-ok").onclick = () => $("confirm-dialog").close("yes");
async function copy(text) { try { await navigator.clipboard.writeText(text); toast("已复制"); } catch { throw new Error("当前浏览器不允许自动复制，请手动选择复制；远程访问建议使用 HTTPS"); } }
function badge(value) { const map = {success:"成功", failed:"失败", cancelled:"已中断", healthy:"刷新正常", refresh_failed:"刷新失败", unknown:"未检测"}; return el("span", map[value] || value, `badge ${value === "healthy" ? "success" : value === "refresh_failed" ? "failed" : value}`); }
function table(id, headings, rows) {
  const parent = clear(id); if (!rows.length) { parent.append(el("div", "暂无记录。", "empty")); return; }
  const node = el("table"), head = el("thead"), tr = el("tr");
  headings.forEach(h => tr.append(el("th", h))); head.append(tr); node.append(head);
  const body = el("tbody");
  rows.forEach(row => { const tr = el("tr"); row.forEach(value => { const td = el("td"); if (value instanceof Node) td.append(value); else td.textContent = value == null ? "—" : value; tr.append(td); }); body.append(tr); });
  node.append(body); parent.append(node);
}
function metrics(id, s) {
  const parent = clear(id); const values = [
    ["请求次数", number(s.requests), `成功 ${number(s.success)} · 失败 ${number(s.failed)} · 中断 ${number(s.cancelled)}`, "data_histogram"],
    ["成功率", s.requests ? `${number(s.success / s.requests * 100)}%` : "—", "按完整请求结果统计", "checkmark_circle"],
    ["估算 Token", number(s.input_tokens + s.output_tokens), `输入 ${number(s.input_tokens)} · 输出 ${number(s.output_tokens)}`, "sparkle"],
    ["平均耗时", ms(s.avg_ms), `首段内容 ${ms(s.first_ms)} · 图片 ${number(s.images)} 张`, "clock"]
  ];
  values.forEach(([label, value, note, iconName]) => {
    const card = el("article", null, "metric"), top = el("div", null, "metric-top");
    const symbol = el("span", null, "metric-icon"); symbol.append(icon(iconName));
    top.append(el("span", label), symbol);
    card.append(top, el("div", value, "metric-value"), el("div", note, "metric-note")); parent.append(card);
  });
}
function chart(id, trends) {
  const parent = clear(id); if (!trends.length) { parent.append(el("div", "还没有调用数据 · 连接客户端后，趋势会出现在这里")); return; }
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", "0 0 640 210"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "请求量趋势图");
  const make = (tag, attrs, text) => { const n = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k,v]) => n.setAttribute(k, String(v))); if (text) n.textContent = text; svg.append(n); return n; };
  const grouped = []; const chunk = Math.max(1, Math.ceil(trends.length/48));
  for (let i=0;i<trends.length;i+=chunk) grouped.push({bucket:trends[i].bucket,requests:trends.slice(i,i+chunk).reduce((n,t)=>n+t.requests,0)});
  const groupMax = Math.max(...grouped.map(t=>t.requests),1), step = 585/grouped.length;
  [0,1,2,3].forEach(i => { const y = 15 + i * 53; make("line", {x1:35,y1:y,x2:630,y2:y,class:"gridline"}); make("text", {x:0,y:y+4}, number(groupMax * (3-i)/3)); });
  grouped.forEach((t,i) => { const height = t.requests/groupMax*150; const bar=make("rect",{x:40+i*step,y:174-height,width:Math.max(2,Math.min(24,step-4)),height,rx:3,class:"bar"}); const title=document.createElementNS(ns,"title"); title.textContent=`${date(t.bucket)} 起 ${chunk} 小时：${t.requests} 次`; bar.append(title); if(i===0||i===grouped.length-1||i===Math.floor(grouped.length/2)) make("text",{x:40+i*step,y:199,"text-anchor":i===grouped.length-1?"end":"start"},new Date(t.bucket*1000).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",timeZone:"Asia/Shanghai"})); });
  parent.append(svg);
}
function requestTable(id, rows, full=false) {
  table(id, ["时间", "模型 / 协议", "结果", "Token（入 / 出）", "耗时", ...(full?["密钥 / 账号", "请求 ID / 错误"]:[])], rows.map(r => {
    const identity=el("div",r.model||"—"); identity.append(el("div",r.protocol,"muted small"));
    const result=el("div"); result.append(badge(r.outcome),el("div",`HTTP ${r.status}`,"muted small"));
    const row=[date(r.created),identity,result,`${number(r.input_tokens)} / ${number(r.output_tokens)}`,ms(r.duration_ms)];
    if(full){const key=el("div",r.key_id);key.append(el("div",r.account||"—","muted small"));const detail=el("div");detail.append(el("code",r.id.slice(0,12)),el("div",r.error||"—","muted small"));row.push(key,detail);} return row;
  }));
}
async function overview() {
  const d=await api("/overview"); metrics("overview-metrics",d.today.summary);chart("overview-chart",d.today.trends);requestTable("recent-table",d.today.rows.slice(0,5));
  $("uptime").textContent=`${Math.floor(d.uptime/3600)} 小时 ${Math.floor(d.uptime/60)%60} 分钟`;
  $("sidebar-status").textContent=`${d.active} 个进行中请求`;
  const info=clear("runtime-info");
  [["进行中 / 并发上限",`${d.active} / ${d.concurrency}`],["真实账号",d.accounts.filter(a=>!a.guest).length],["保留期内累计请求",number(d.total.requests)],["统计保留",`${d.retention_days} 天`],["官方剩余额度","未知"]].forEach(([k,v])=>{const div=el("div");div.append(el("dt",k),el("dd",v));info.append(div);});
  const ready = d.accounts.length > 0;
  $("service-state").textContent = ready ? "已接入官方账号" : "等待官方账号登录";
  document.querySelector(".hero-strip .status-dot").classList.toggle("waiting", !ready);
  const warnings=clear("warnings");
  if(!d.api_auth_enabled)warnings.append(el("div","当前反代接口未启用调用密钥。创建第一个 API Key 后，客户端需要携带密钥。","notice danger"));
  if(!d.accounts.some(a=>!a.guest))warnings.append(el("div","尚未接入官方账号，反代调用已暂停。请先在“上游账号”中登录或导入凭据。","notice danger"));
  if(d.persistence_error||d.metrics_error)warnings.append(el("div","数据写入失败，请检查数据目录权限与磁盘空间；最近凭据或统计可能未持久保存。","notice danger"));
}
function localInput(d){return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
$("usage-filter").elements.since.value=localInput(new Date(Date.now()-7*86400000));
$("usage-filter").elements.until.value=localInput(new Date());
async function usage() {
  const form=new FormData($("usage-filter")),params=new URLSearchParams();
  for(const [key,value] of form)if(value)params.set(key,["since","until"].includes(key)?String(new Date(value).getTime()/1000):value);
  params.set("page",String(state.usagePage));const d=await api(`/usage?${params}`);
  metrics("usage-metrics",d.summary);chart("usage-chart",d.trends);requestTable("usage-table",d.rows,true);
  $("usage-page").textContent=`第 ${d.page} 页 · 共 ${number(d.summary.requests)} 条`;$("usage-prev").disabled=d.page<=1;$("usage-next").disabled=d.page*50>=d.summary.requests;
  table("usage-groups",["模型","协议","密钥","请求","成功","估算 Token"],d.groups.map(g=>[g.model||"—",g.protocol,g.key_id,g.requests,g.success,number(g.input_tokens+g.output_tokens)]));
}
async function loadUsageKeys(){const d=await api("/keys");const select=clear("filter-key");[["","全部密钥"],["anonymous","未携带密钥"],["panel-test","面板测试"],["panel-chat","面板聊天"],...d.keys.map(k=>[k.id,k.name]),...Array.from({length:d.environment_key_count},(_,i)=>[`environment-${i+1}`,`环境密钥 ${i+1}`])].forEach(([value,label])=>{const o=el("option",label);o.value=value;select.append(o);});}
async function accounts(){const d=await api("/accounts");table("accounts-table",["账号","凭据","刷新状态","上次检查","本地缓存截止","操作"],d.accounts.map(a=>{
  const actions=el("div");if(!a.guest){actions.append(action("检查",async()=>{const r=await api(`/accounts/${a.id}/check`,{method:"POST"});toast(r.health==="healthy"?"Token 刷新成功":"Token 刷新失败，请检查账号和网络");await accounts();}),action("移除",async()=>{if(await confirmAction("移除账号？","这会移除本服务保存的凭据，不会注销官方账号。")){await api(`/accounts/${a.id}`,{method:"DELETE"});await accounts();}}));}
  return [a.guest?"游客槽位":a.label,a.token,badge(a.health),date(a.checked_at),date(a.cache_until),actions];}));}
async function keys(){const d=await api("/keys");$("env-keys").textContent=`另有 ${d.environment_key_count} 个部署配置密钥；只能从部署配置修改。列表里的密钥仅存储摘要。`;
  table("keys-table",["名称","前缀","状态","创建时间","操作"],d.keys.map(k=>{const actions=el("div");actions.append(action("查看用量",async()=>{await navigate("usage");$("filter-key").value=k.id;await usage();}),action(k.enabled?"禁用":"启用",async()=>{if(await confirmAction(`${k.enabled?"禁用":"启用"}密钥？`,k.name)){await api(`/keys/${k.id}`,{method:"PATCH",body:{enabled:!k.enabled}});await keys();}}));return [k.name,`${k.prefix}…`,badge(k.enabled?"success":"cancelled"),date(k.created),actions];}));}
const configLabels={glm_base_url:"上游 HTTPS 基址",glm_timeout:"请求超时（秒）",glm_max_concurrency:"最大并发",glm_delete_conversation:"请求结束后清理上游会话",proxy:"出站 HTTP / HTTPS 代理",chat_assistant_id:"对话助手 ID",image_assistant_id:"生图助手 ID",image_model_name:"生图模型名称",panel_retention_days:"统计与日志保留天数"};
async function config(){const d=await api("/config");state.config=d;const fields=clear("config-fields");Object.entries(d.values).forEach(([name,value])=>{const label=el("label",configLabels[name]);let input;
  if(typeof value==="boolean"){input=el("select");[["true","开启"],["false","关闭"]].forEach(([v,t])=>{const o=el("option",t);o.value=v;input.append(o);});input.value=String(value);}else{input=el("input");input.type=name==="proxy"?"password":typeof value==="number"?"number":"text";input.value=value;input.autocomplete="off";if(name==="proxy")input.placeholder=d.proxy_configured?"已配置，留空保持原值":"未配置，留空不使用代理";}
  input.name=name;input.disabled=d.locked.includes(name);label.append(input);if(input.disabled)label.append(el("span","由环境变量或 .env 管理","muted"));fields.append(label);
  if(name==="proxy"){const clearLabel=el("label","清除已保存代理");const select=el("select");select.name="clear_proxy";[["no","保留"],["yes","清除"]].forEach(([v,t])=>{const o=el("option",t);o.value=v;select.append(o);});select.disabled=d.locked.includes("proxy");clearLabel.append(select);fields.append(clearLabel);}
  });const s=await api("/session");$("password-form").hidden=s.password_managed_by_env;$("password-note").textContent="修改后所有管理会话失效，需要重新登录。首次密码文件不会随之更新。";
}
async function audit(){const d=await api(`/audit?page=${state.auditPage}`);table("audit-table",["时间","操作","变更字段 / 结果"],d.rows.map(r=>[date(r.created),r.action,r.detail]));$("audit-page").textContent=`第 ${d.page} 页`;$("audit-prev").disabled=d.page<=1;$("audit-next").disabled=d.rows.length<50;}
async function testPage(){
  const d = await api("/accounts");
  const ready = d.accounts.length > 0;
  $("send-test").disabled = !ready;
  $("test-status").textContent = ready ? "" : "请先在上游账号中登录";
  await models();
}
async function models(){if($("test-model").options.length)return;const d=await api("/models");d.data.forEach(m=>{const option=el("option",m.id);option.value=m.id;$("test-model").append(option);});}
function clientConfig(){const raw=$("client-base").value.replace(/\/$/,"");let parsed;try{parsed=new URL(raw);}catch{return;}if(!["https:","http:"].includes(parsed.protocol))return;const base=parsed.href.replace(/\/$/,""),key=$("client-key").value||"YOUR_API_KEY",model=$("test-model").value||"glm-4";
  const quote=s=>"'"+s.replaceAll("'","'\\''")+"'";
  const code={openai:`from openai import OpenAI\n\nclient = OpenAI(\n    base_url=${JSON.stringify(base+"/v1")},\n    api_key=${JSON.stringify(key)},\n)\nresult = client.chat.completions.create(\n    model=${JSON.stringify(model)},\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(result.choices[0].message.content)`,claude:`# Bash / zsh\nexport ANTHROPIC_BASE_URL=${quote(base)}\nexport ANTHROPIC_AUTH_TOKEN=${quote(key)}\nclaude --model ${quote(model)}`,curl:`curl ${quote(base+"/v1/chat/completions")} \\\n  -H ${quote("Authorization: Bearer "+key)} \\\n  -H 'Content-Type: application/json' \\\n  -d ${quote(JSON.stringify({model,messages:[{role:"user",content:"你好"}]}))}`,cherry:`提供商：OpenAI 兼容\nAPI Base URL：${base}/v1\nAPI Key：${key}\n模型：${model}`,gemini:`POST ${base}/v1beta/models/${encodeURIComponent(model)}:generateContent\nx-goog-api-key: ${key}\nContent-Type: application/json\n\n${JSON.stringify({contents:[{role:"user",parts:[{text:"你好"}]}]},null,2)}`};$("client-code").textContent=code[$("client-type").value];}
async function connect(){await models();if(!$("client-base").value)$("client-base").value=location.origin;const parent=clear("endpoints");[["OpenAI Chat / Responses","/v1"],["Anthropic Messages","/v1/messages"],["Gemini","/v1beta/models/{model}:generateContent"],["图片生成","/v1/images/generations"]].forEach(([label,path])=>{const row=el("div",null,"endpoint");row.append(el("span",label),el("code",location.origin+path));parent.append(row);});clientConfig();}
async function navigate(page){if(state.navigating)return;if(!pages[page])page="overview";state.page=page;$("page-error").hidden=true;document.querySelectorAll(".page").forEach(n=>n.hidden=n.id!==`page-${page}`);document.querySelectorAll("[data-page]").forEach(n=>{n.classList.toggle("selected",n.dataset.page===page);n.setAttribute("aria-current",n.dataset.page===page?"page":"false");});const [label,title,description]=pages[page];$("breadcrumb").textContent=label;$("page-title").textContent=title;$("page-description").textContent=description;history.replaceState(null,"",`#${page}`);state.navigating=true;const target=$(`page-${page}`);target.inert=true;target.setAttribute("aria-busy","true");try{if(page==="usage")await loadUsageKeys();await refresh();}finally{target.inert=false;target.setAttribute("aria-busy","false");state.navigating=false;}}
async function refresh(){const tasks={overview,usage,accounts,keys,config,test:testPage,chat:chatPage,connect,audit};await tasks[state.page]();$("updated").textContent=`更新于 ${new Date().toLocaleTimeString("zh-CN",{hour12:false})}`;}
document.querySelectorAll("[data-page],[data-go]").forEach(n=>n.addEventListener("click",()=>navigate(n.dataset.page||n.dataset.go).catch(e=>error(e.message))));
bind("refresh","click",refresh);
bind("login-form","submit",async()=>{try{const d=await api("/login",{method:"POST",body:{password:$("login-form").elements.password.value}});state.csrf=d.csrf;$("login-form").reset();$("login-error").textContent="";$("app").hidden=false;$("login-view").hidden=true;await navigate(location.hash.slice(1)||"overview");}catch(e){$("login-error").textContent=e.message;}});
async function logout(){await api("/logout",{method:"POST"});if(state.test)state.test.abort();if(state.chatAbort)state.chatAbort.abort();$("client-key").value="";$("new-key").textContent="";$("new-key-box").hidden=true;loggedOut();}
bind("logout","click",logout);bind("logout-mobile","click",logout);
bind("usage-filter","submit",async()=>{state.usagePage=1;await usage();});bind("usage-prev","click",async()=>{state.usagePage=Math.max(1,state.usagePage-1);await usage();});bind("usage-next","click",async()=>{state.usagePage++;await usage();});
bind("audit-prev","click",async()=>{state.auditPage=Math.max(1,state.auditPage-1);await audit();});bind("audit-next","click",async()=>{state.auditPage++;await audit();});
bind("import-form","submit",async()=>{const token=$("import-form").elements.refresh_token.value;const d=await api("/accounts",{method:"POST",body:{refresh_token:token}});$("import-form").reset();toast(d.added?"账号已保存并接入":"账号已存在");await accounts();});
bind("key-form","submit",async()=>{const d=await api("/keys",{method:"POST",body:{name:$("key-form").elements.name.value}});$("key-form").reset();$("new-key-box").hidden=false;$("new-key").textContent=d.key;await keys();});bind("copy-key","click",()=>copy($("new-key").textContent));bind("dismiss-key","click",()=>{$("new-key").textContent="";$("new-key-box").hidden=true;});
bind("config-form","submit",async()=>{const patch={},preview=[];for(const [name,value]of new FormData($("config-form"))){if(name==="clear_proxy")continue;const old=state.config.values[name];if(name==="proxy"&&!value)continue;const converted=typeof old==="boolean"?value==="true":typeof old==="number"?Number(value):value;if(converted!==old){patch[name]=converted;preview.push(`${configLabels[name]}：${name==="proxy"?"替换代理（隐藏内容）":`${old} → ${converted}`}`);}}if($("config-form").elements.clear_proxy.value==="yes"){patch.proxy="";preview.push("出站代理：清除");}if(!Object.keys(patch).length)return toast("没有配置变更");if(await confirmAction("保存并立即生效？",preview.join("\n"))){await api("/config",{method:"PATCH",body:patch});toast("配置已保存并生效");await config();}});
bind("password-form","submit",async()=>{const data=Object.fromEntries(new FormData($("password-form")));await api("/password",{method:"POST",body:data});$("password-form").reset();loggedOut();toast("密码已修改，请重新登录");});
function clearLoginImages() {
  $("official-image").hidden = true; $("official-image").removeAttribute("src");
  $("official-qr-box").hidden = true; $("official-qr").removeAttribute("src");
}
function loginButtons(busy) {
  $("login-local").disabled = busy; $("login-remote").disabled = busy;
  $("cancel-login").hidden = !busy;
}
async function startLogin(mode) {
  if (state.login) return;
  loginButtons(true); clearLoginImages();
  try {
    const d = await api("/browser-login", {method:"POST", body:{mode}});
    state.login = d.id; $("official-login").hidden = false;
    $("official-help").hidden = mode !== "remote";
    await pollLogin();
  } catch (e) { loginButtons(false); throw e; }
}
async function pollLogin() {
  clearTimeout(state.loginTimer);
  const id = state.login;
  if (!id || !state.csrf) return;
  try {
    const d = await api(`/browser-login/${id}`);
    if (state.login !== id) return;
    const pending = ["starting", "waiting"].includes(d.status);
    $("official-status").textContent = d.message + (pending ? ` · 剩余 ${Math.max(0, Math.ceil(d.expires-Date.now()/1000))} 秒` : "");
    if (pending && d.has_image && d.mode === "remote") {
      $("official-image").src = `/panel/api/browser-login/${id}/image?t=${Date.now()}`;
      $("official-image").hidden = false;
    } else { $("official-image").hidden = true; $("official-image").removeAttribute("src"); }
    if (pending && d.has_qr && d.mode === "remote") {
      $("official-qr").src = `/panel/api/browser-login/${id}/qr?t=${Date.now()}`;
      $("official-qr-box").hidden = false;
    } else { $("official-qr-box").hidden = true; $("official-qr").removeAttribute("src"); }
    if (pending) { state.loginTimer = setTimeout(pollLogin, 2000); return; }
    clearLoginImages(); state.login = null; loginButtons(false);
    await accounts();
    if (d.status === "completed") toast("官方账号已保存并接入反代");
  } catch (e) {
    if (state.login !== id || !state.csrf) return;
    $("official-status").textContent = `暂时无法查询登录结果：${e.message}，正在重试`;
    state.loginTimer = setTimeout(pollLogin, 5000);
  }
}
bind("login-local", "click", () => startLogin("local"));
bind("login-remote", "click", () => startLogin("remote"));
bind("cancel-login", "click", async () => {
  if (state.login) await api(`/browser-login/${state.login}`, {method:"DELETE"});
  clearTimeout(state.loginTimer); state.login = null; clearLoginImages(); loginButtons(false);
  $("official-status").textContent = "登录已取消";
});
bind("official-image-button","click",async(event)=>{if(!state.login||$("official-image").hidden)return;const rect=$("official-image").getBoundingClientRect();await api(`/browser-login/${state.login}/click`,{method:"POST",body:{x:Math.min(999,Math.max(0,(event.clientX-rect.left)/rect.width*1000)),y:Math.min(739,Math.max(0,(event.clientY-rect.top)/rect.height*740))}});});
bind("test-form","submit",async()=>{state.test=new AbortController();$("stop-test").disabled=false;$("test-output").textContent="";$("test-reasoning").textContent="";$("test-usage").textContent="";$("test-status").textContent="正在请求…";const start=performance.now();let failed=false;try{const response=await fetch("/panel/api/test",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:JSON.stringify(Object.fromEntries(new FormData($("test-form")))),signal:state.test.signal});if(!response.ok){const d=await response.json();throw new Error(d.detail||d.error?.message||"测试失败");}const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let index;while((index=buffer.indexOf("\n\n"))>=0){const frame=buffer.slice(0,index);buffer=buffer.slice(index+2);for(const line of frame.split("\n")){if(!line.startsWith("data: "))continue;const text=line.slice(6);if(text==="[DONE]")continue;const data=JSON.parse(text);if(data.error){failed=true;throw new Error(data.error.message);}const delta=data.choices?.[0]?.delta||{};if(delta.content)$("test-output").textContent+=delta.content;if(delta.reasoning_content)$("test-reasoning").textContent+=delta.reasoning_content;if(data.usage)$("test-usage").textContent=`估算 Token：输入 ${data.usage.prompt_tokens} · 输出 ${data.usage.completion_tokens}`;}}}$("test-status").textContent=`完成 · ${ms(performance.now()-start)}`;}catch(e){failed=true;$("test-status").textContent=e.name==="AbortError"?"已停止":`失败：${e.message}`;if(e.name!=="AbortError")error(e.message);}finally{state.test=null;$("stop-test").disabled=true;if(!failed&&!$("test-output").textContent)$("test-output").textContent="上游未返回正文。";}});
bind("stop-test","click",()=>state.test?.abort());["client-type","client-base","client-key"].forEach(id=>$(id).addEventListener("input",clientConfig));bind("copy-client","click",()=>copy($("client-code").textContent));
function setChatMode(mode){state.chatMode=mode;$("mode-chat").classList.toggle("selected",mode==="chat");$("mode-agent").classList.toggle("selected",mode==="agent");$("chat-mode-note").textContent=`当前模式：${mode==="agent"?"Agent（内置工具 + 联网）":"对话（标准聊天）"}`;}
function chatBubble(role){const wrap=el("div",null,`chat-msg ${role}`);const meta=el("div",null,"chat-meta");meta.append(el("span",role==="user"?"你":(state.chatMode==="agent"?"Agent":"助手")));wrap.append(meta);const text=el("div","","chat-text");wrap.append(text);return{wrap,meta,text};}
function resetChatLog(){const log=clear("chat-log");log.append(el("div","开始对话吧。在上方可切换“对话 / Agent”模式。","chat-empty"));}
function autoGrow(node){node.style.height="auto";node.style.height=Math.min(node.scrollHeight,200)+"px";}
function scrollChat(){const log=$("chat-log");log.scrollTop=log.scrollHeight;}
async function chatPage(){const d=await api("/accounts");state.chatReady=d.accounts.length>0;$("chat-send").disabled=!state.chatReady||state.chatBusy;$("chat-status").textContent=state.chatReady?"":"请先在上游账号中登录";if(!$("chat-model").options.length){const m=await api("/models");m.data.forEach(x=>{const o=el("option",x.id);o.value=x.id;$("chat-model").append(o);});}if(state.chatMode==null){const ov=await api("/overview");setChatMode(ov.agent_mode?"agent":"chat");}if(!$("chat-log").children.length)resetChatLog();}
async function sendChat(){const input=$("chat-input");const text=input.value.trim();if(!text||state.chatBusy||!state.chatReady)return;const model=$("chat-model").value||"glm-5.3";const mode=state.chatMode==="agent"?"agent":"chat";state.chatMessages.push({role:"user",content:text});if($("chat-log").firstElementChild?.className==="chat-empty")$("chat-log").replaceChildren();const userBubble=chatBubble("user");userBubble.text.textContent=text;$("chat-log").append(userBubble.wrap);const assistant=chatBubble("assistant");$("chat-log").append(assistant.wrap);input.value="";autoGrow(input);state.chatBusy=true;state.chatAbort=new AbortController();$("chat-send").disabled=true;$("chat-status").textContent="正在生成…";const start=performance.now();let reasoning="",usageText="";try{const response=await fetch("/panel/api/chat",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:JSON.stringify({model,mode,messages:state.chatMessages}),signal:state.chatAbort.signal});if(!response.ok){const d=await response.json().catch(()=>({}));throw new Error(d.detail||d.error?.message||"请求失败");}const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";while(true){const{value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let index;while((index=buffer.indexOf("\n\n"))>=0){const frame=buffer.slice(0,index);buffer=buffer.slice(index+2);for(const line of frame.split("\n")){if(!line.startsWith("data: "))continue;const payload=line.slice(6);if(payload==="[DONE]")continue;let data;try{data=JSON.parse(payload);}catch{continue;}if(data.error)throw new Error(data.error.message);const delta=data.choices?.[0]?.delta||{};if(delta.reasoning_content)reasoning+=delta.reasoning_content;if(delta.content)assistant.text.textContent+=delta.content;if(data.usage)usageText=`输入 ${data.usage.prompt_tokens} · 输出 ${data.usage.completion_tokens}`;}}}if(reasoning){const details=el("details",null,"chat-reasoning");details.append(el("summary","思考内容"));details.append(el("div",reasoning,"chat-reasoning-body"));assistant.wrap.insertBefore(details,assistant.text);}const finalText=assistant.text.textContent.trim()||"（未返回正文）";state.chatMessages.push({role:"assistant",content:finalText});$("chat-status").textContent=`完成 · ${ms(performance.now()-start)}${usageText?" · "+usageText:""}`;}catch(e){if(e.name!=="AbortError"){$("chat-status").textContent=`失败：${e.message}`;if(!assistant.text.textContent)assistant.text.textContent="（请求失败）";error(e.message);}else{$("chat-status").textContent="已停止";}}finally{state.chatBusy=false;state.chatAbort=null;$("chat-send").disabled=!state.chatReady;input.focus();scrollChat();}}
bind("mode-chat","click",()=>setChatMode("chat"));bind("mode-agent","click",()=>setChatMode("agent"));bind("chat-send","click",sendChat);bind("chat-clear","click",()=>{if(state.chatBusy)state.chatAbort?.abort();state.chatMessages=[];resetChatLog();$("chat-status").textContent="";});
$("chat-input").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendChat();}});$("chat-input").addEventListener("input",e=>autoGrow(e.target));
api("/session").then(async d=>{state.csrf=d.csrf;$("app").hidden=false;$("login-view").hidden=true;await navigate(location.hash.slice(1)||"overview");}).catch(()=>loggedOut());
