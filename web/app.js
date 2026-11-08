// Headroom UI logic (served as a file so the page can use a strict Content-Security-Policy).
const $=id=>document.getElementById(id);
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const IC={sun:"",moon:"",max:"",restore:""};
const fmt=(v,d=0)=>v==null||isNaN(v)?"–":(+v).toFixed(d);
const sg=(v,d=1)=>v==null||isNaN(v)?"–":(v>=0?"+":"")+(+v).toFixed(d);
let S=null, seq=0, logs=[], profiles=null;

function toast(m,ms=3800){ const t=$("toast"); t.textContent=m; t.classList.add("show"); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("show"),ms); }
// Headroom.exe adds the per-launch token header to this window's requests (the page itself never
// sees it); the backend refuses anything without it. POSTs are JSON only.
async function api(u,body){
  const o=body!==undefined?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{};
  const r=await fetch(u,o); const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText); return j; }
async function act(u,body,okMsg){ try{ const r=await api(u,body||{}); if(okMsg) toast(okMsg); poll(); return r; }catch(e){ toast(e.message,5000); throw e; } }
function ask(title,text,{yes="OK",no="Cancel",icon="",danger=false}={}){
  return new Promise(res=>{ $("dlgT").textContent=title; $("dlgB").textContent=text; $("dlgIc").textContent=icon;
    $("dlgYes").textContent=yes; $("dlgNo").textContent=no; $("dlgNo").style.display=no?"":"none";
    $("dlgYes").className="btn "+(danger?"red":"pri"); $("dlg").classList.add("show");
    const done=v=>{ $("dlg").classList.remove("show"); res(v); };
    $("dlgYes").onclick=()=>done(true); $("dlgNo").onclick=()=>done(false); });
}

// ---- theme + tabs
function setTheme(t){ document.documentElement.dataset.theme=t; localStorage.setItem("theme",t); $("themeIc").textContent=t==="dark"?IC.sun:IC.moon; if(S) rings(); }
$("themeBtn").onclick=()=>setTheme(document.documentElement.dataset.theme==="dark"?"light":"dark");
function tab(n){ document.querySelectorAll("#tabs span").forEach(s=>s.classList.toggle("sel",s.dataset.tab===n));
  document.querySelectorAll(".view").forEach(v=>v.classList.toggle("sel",v.id==="v-"+n)); localStorage.setItem("tab",n); if(n==="prof") renderProfiles(); if(n==="setup"&&S) renderSetup(true); }
document.querySelectorAll("#tabs span").forEach(s=>s.onclick=()=>tab(s.dataset.tab));
$("folderBtn").onclick=()=>api("/api/openfolder",{});

// ---- rings
function ring(el, frac, val, unit, name, stops){
  const r=22, c=2*Math.PI*r, arc=c*0.75, f=Math.max(0,Math.min(1,frac||0)), id=el.id+"g";
  el.innerHTML=`<svg viewBox="0 0 54 54"><defs><linearGradient id="${id}" x1="0" y1="1" x2="1" y2="0">${stops.map((s,i)=>`<stop offset="${i/(stops.length-1)}" stop-color="${s}"/>`).join("")}</linearGradient></defs>
    <circle cx="27" cy="27" r="${r}" fill="none" stroke="rgba(${css("--ink")},.1)" stroke-width="5" stroke-linecap="round" stroke-dasharray="${arc} ${c}"/>
    <circle cx="27" cy="27" r="${r}" fill="none" stroke="url(#${id})" stroke-width="5" stroke-linecap="round" stroke-dasharray="${Math.max(0.01,arc*f)} ${c}"/></svg>
    <div class="v"><span style="font-size:${String(val).length>3?11:13}px">${val}<small>${unit}</small></span></div><div class="n">${name}</div>`;
}
function rings(){
  const c=S.sensors.cpu||{}, g=S.sensors.gpu||{}, x=S.sensors.gpu_x||{};
  const L=[css("--cyan"),css("--indigo"),css("--purple"),css("--pink")], H=[css("--green"),"#A3E635",css("--amber"),"#FB923C",css("--red")];
  ring($("rCpuL"), c.load!=null?c.load/100:0, c.load!=null?Math.round(c.load):"–", "%", "CPU LOAD", L);
  ring($("rCpu"), c.tdie!=null?(c.tdie-25)/(S.limits.cpu-25):0, c.tdie!=null?Math.round(c.tdie):"–", "°C", "CPU TEMP", H);
  ring($("rGpuL"), g.util!=null?g.util/100:0, g.util??"–", "%", "GPU LOAD", L);
  ring($("rGpu"), g.temp!=null?(g.temp-25)/(S.limits.gpu_core-25):0, g.temp??"–", "°C", "GPU TEMP", H);
  ring($("rMem"), x.mem_junction!=null?(x.mem_junction-25)/(S.limits.gpu_mem-25):0, x.mem_junction!=null?Math.round(x.mem_junction):"–", "°C", "VRAM TEMP", H);
  ring($("rPow"), g.power!=null?g.power/(g.limit||300):0, g.power!=null?Math.round(g.power):"–", "W", "GPU PWR", L);
}

// ---- uplift maths (same as headroom/uplift.py)
const pct=(n,o)=>o?(n/o-1)*100:0;
const mid=(r,d)=>Array.isArray(r)&&r.length>1?Math.round((r[0]+r[1])/2):d;
function predictGpu(core,memAB,stock){ const c=core*0.8/(stock||2800)*100, m=memAB/((S&&S.profile&&S.profile.gpu_mem_max)||14000)*100; return {c,m,g:0.65*c+0.20*m,lo:0.45*c+0.10*m,hi:0.85*c+0.25*m}; }
function predictCpu(boost,co){ const st=boost/((S&&S.profile&&S.profile.cpu_stock_boost)||5000)*100*0.9, mt=Math.min(4.5,Math.abs(co)*0.12); return {st,mt,cb:0.5*st,gb:0.1*st}; }

// ---- dashboard
function stat(label,val,cls=""){ return `<div class="stat"><span>${esc(label)}</span><b class="${cls}">${val}</b></div>`; }
const lvl=(v,w,b)=>v==null?"":v>=b?"bad":v>=w?"warn":"";
function renderDash(){
  const c=S.sensors.cpu||{}, g=S.sensors.gpu||{}, x=S.sensors.gpu_x||{}, f=S.sensors.fans||{}, L=S.limits;
  $("dCpuName").textContent=S.hw.cpu; $("dBoard").textContent=`${S.hw.board} · BIOS ${S.hw.bios}`; $("dGpuName").textContent=S.hw.gpu; $("dRam").textContent=S.hw.ram;
  const P=S.profile||{}; $("dGpuSub").textContent=P.gpu_sub||""; $("dCooler").textContent=P.cooler_name&&P.cooler_name!=="CPU cooler"?P.cooler_name+" · ":"";
  $("dCpu").innerHTML=S.sensors.lhm_error?`<div class="hint bad">LibreHardwareMonitor isn't answering on :8085, so CPU sensors are unavailable.</div>`:
    stat("Temperature",fmt(c.tdie,1)+" °C",lvl(c.tdie,L.cpu_warn,L.cpu))+stat("Package power",fmt(c.package_w,1)+" W")+
    stat("Peak core clock",fmt(c.max_core_clock)+" MHz")+stat("Avg effective clock",fmt(c.eff_avg)+" MHz")+stat("Vcore",fmt(c.vcore,3)+" V")+stat("Load",fmt(c.load)+" %");
  const thr=(g.throttle||[]).filter(t=>t!=="idle").join(", ")||"nothing";
  $("dGpu").innerHTML=stat("Core temperature",fmt(g.temp)+" °C",lvl(g.temp,L.gpu_core_warn,L.gpu_core))+
    stat("Memory junction",fmt(x.mem_junction)+" °C",lvl(x.mem_junction,L.gpu_mem_warn,L.gpu_mem))+
    stat("Power / limit",`${fmt(g.power)} / ${fmt(g.limit)} W`)+stat("Core clock",fmt(g.clk)+" MHz")+
    stat("Memory clock",`${fmt(g.mem_clk)} MHz · ${fmt((g.mem_clk||0)*2/1000,1)} Gbps`)+stat("Limited by",thr,thr==="nothing"?"":"warn");
  const ap=S.gpu.applied, eff=S.gpu.efficiency, cl=P.cooler||{}, pmin=(cl.preflight||{}).pump_min_rpm||1000;
  const pump=cl.pump_sensor?f[cl.pump_sensor]:null, rad=cl.radiator_sensor?f[cl.radiator_sensor]:null;
  const other=Object.entries(f).filter(([k,v])=>v>0&&k!==cl.pump_sensor&&k!==cl.radiator_sensor).slice(0,2);
  $("dCool").innerHTML=(cl.pump_sensor?stat(`Pump (${cl.pump_sensor})`,fmt(pump)+" rpm",pump!=null&&pump<pmin?"bad":"good"):"")+
    (cl.radiator_sensor?stat(`CPU fans (${cl.radiator_sensor})`,fmt(rad)+" rpm"):"")+
    (!cl.pump_sensor&&!cl.radiator_sensor?other.map(([k,v])=>stat(k,fmt(v)+" rpm")).join(""):"")+
    stat("GPU fans",`${fmt(g.fan)} % · ${(x.fans||[]).map(v=>fmt(v)).join(" / ")} rpm`)+
    stat("GPU overclock",ap?`core ${sg(ap.core,0)} · mem ${sg(ap.mem*S.gpu.mem_ratio,0)} AB`+(eff?` · capped ${eff.cap}`:""):"stock",ap?"good":"")+
    stat("CPU tuning step",S.cpu.phase)+stat("Rights",S.admin?"administrator":"read-only",S.admin?"good":"warn");
  // uplift
  const gr=S.gpu.result, cr=S.cpu.result; let h="";
  if(gr){ const u=gr.uplift, m=u.measured; h+=upCard("GPU · tuned "+gr.when, u.estimated_games_pct, u.estimated_range_pct, "estimated in games",
      [["Core clock under load",sg(m.core_clock_pct)+"%"],["Compute throughput",sg(m.compute_pct)+"%"],["VRAM bandwidth",sg(m.mem_bandwidth_pct)+"%"],["Offsets",`core ${sg(gr.core,0)} · mem ${sg(gr.mem_afterburner,0)} AB`]]);
  } else if(S.caps.gpu_tuning){ const t=P.gpu_typical||{}, tc=mid(t.core_offset_mhz,150), tm=mid(t.mem_offset_afterburner,500), e=S.gpu.expected||{};
      const p=predictGpu(tc,tm,(S.gpu.baseline||{}).eff_clk); h+=upCard("GPU · not tuned yet",p.g,[p.lo,p.hi],`typical for this card (${sg(tc,0)} / ${sg(tm,0)})`,
      [["Expected in games",e.games?`+${e.games.join("–")}%`:"–"],...(P.gpu_results||[]).slice(0,1).map(r=>[r.source,r.result.slice(0,60)])],true);
  } else h+=upCard("GPU · not tunable here",0,[0,0],P.gpu_method||"GPU tuning needs an NVIDIA RTX card",[],true);
  if(cr){ const u=cr.uplift, m=u.measured; h+=upCard("CPU · tuned "+cr.when, u.estimated_cpu_bound_games_pct, [u.estimated_gpu_bound_games_pct,u.estimated_cpu_bound_games_pct],"CPU-bound games",
      [["Single-thread",sg(m.single_thread_pct)+"%"],["Multi-thread",sg(m.multi_thread_pct)+"%"],["GPU-bound games",sg(u.estimated_gpu_bound_games_pct)+"%"]]);
  } else { const ct=P.cpu_typical||{}, p=predictCpu(ct.boost_override_mhz||0,mid(ct.co_per_core,-20)); h+=upCard("CPU · not tuned yet",p.cb,[p.gb,3],`typical (${ct.boost_override_mhz?"+"+ct.boost_override_mhz+" MHz, ":""}CO ${mid(ct.co_per_core,-20)}), CPU-bound games`,
      [["Single-thread",sg(p.st)+"%"],["Multi-thread",sg(p.mt)+"%"],["1440p / 4K games","+0–1%"]],true); }
  $("dUp").innerHTML=h;
  $("dSafe").innerHTML=`<p class="txt"><b>GPU.</b> Offsets never raise voltage, and the card's own firmware caps its power, so an overclock can crash a game but can't cook the card. The watchdog drops to stock at ${L.gpu_core} °C core or ${L.gpu_mem} °C memory junction.</p>
    <p class="txt"><b>CPU.</b> Curve Optimizer only lowers voltage, and power limits, Vcore and SoC voltage are never raised. The stress test stops at ${L.cpu} °C, or if the pump or radiator fans stop. The BIOS step adds a platform thermal limit as a hardware backstop.</p>
    <p class="txt" style="margin:0"><b>RAM.</b> ${esc(S.profile.mem_verdict||"Memory isn't touched.")}</p>`;
}
function upCard(title,est,range,sub,lines,typ){
  const lo=Math.min(range[0],range[1]), hi=Math.max(range[0],range[1]), sc=v=>Math.max(0,Math.min(100,v/15*100));
  return `<div class="upc"><div class="label">${esc(title)}</div><div class="big ${typ?"typ":""}">${sg(est)}%</div><div class="hint" style="margin-top:0">${esc(sub)}</div>
    <div class="rng"><i style="left:${sc(lo)}%;width:${Math.max(1,sc(hi)-sc(lo))}%"></i><b style="left:calc(${sc(est)}% - 1px)"></b></div>
    <div class="hint" style="display:flex;justify-content:space-between;margin:0;font-size:10.5px"><span>0%</span><span>range ${sg(lo)} … ${sg(hi)}%</span><span>15%</span></div>
    <div class="ml">${lines.map(([a,b])=>`<span>${esc(a)}</span><b>${esc(b)}</b>`).join("")}</div></div>`;
}

// ---- GPU tab
let slidersInit=false;
function renderGpu(){
  const G=S.gpu, p=G.progress||{}, busy=G.busy;
  $("gName").textContent=S.hw.gpu;
  const ok=S.admin&&S.caps.gpu_tuning&&!S.setup.needed; $("gAuto").disabled=busy||!ok; $("gQuick").disabled=busy||!ok; $("gStop").disabled=!busy;
  const ph=p.phase||"idle", job=$("gJob");
  job.className="job "+(busy?"":ph==="done"?"ok":(ph==="abort"||ph==="error")?"err":"idle");
  $("gPh").textContent=busy?ph:({done:"Finished",abort:"Stopped",error:"Error"}[ph]||"Idle");
  if(busy||ph!=="idle") $("gMsg").textContent=p.msg||"";
  $("gBar").style.width=((p.frac||0)*100)+"%"; $("gPct").textContent=busy&&p.frac?Math.round(p.frac*100)+"%":"";
  const r=G.result, b=G.baseline;
  if(r){ const u=r.uplift, m=u.measured;
    $("gRes").innerHTML=`<div class="sec"><span class="label"><i class="ic">&#xE73E;</i>Saved result · ${esc(r.when)}</span></div><div class="up" style="grid-template-columns:auto 1fr;align-items:center">
      <div><div class="big">${sg(u.estimated_games_pct)}%</div><div class="hint" style="margin:0">games, est. ${sg(u.estimated_range_pct[0])} … ${sg(u.estimated_range_pct[1])}%</div></div>
      <div class="ml" style="margin:0"><span>Offsets</span><b>core ${sg(r.core,0)} MHz · memory ${sg(r.mem_afterburner,0)} AB${r.tested?` <span class="hint">(you lowered it · tested ${sg(r.tested.core,0)} / ${sg(r.tested.mem_afterburner,0)})</span>`:""}</b>
      <span>Clock under load</span><b>${fmt(b.eff_clk)} → ${fmt(r.tuned.eff_clk)} MHz (${sg(m.core_clock_pct)}%)</b>
      <span>Compute throughput</span><b>${sg(m.compute_pct)}%</b>
      <span>VRAM bandwidth</span><b>${fmt(b.gbps)} → ${fmt(r.tuned.gbps)} GB/s (${sg(m.mem_bandwidth_pct)}%)</b>
      <span>Power · peak temp</span><b>${fmt(m.power_w[0])} → ${fmt(m.power_w[1])} W · ${fmt(m.temp_c[0])} → ${fmt(m.temp_c[1])} °C</b></div></div>`;
  } else { const t=(S.profile||{}).gpu_typical||{}, rg=(a,u)=>a&&a[1]?`${sg(a[0],0)}…${sg(a[1],0)} ${u}`:null;
    $("gRes").innerHTML=S.caps.gpu_tuning?`<div class="hint">No auto result yet.${t.core_offset_mhz?` Typical for this card: ${[rg(t.core_offset_mhz,"core"),rg(t.mem_offset_afterburner,"memory")].filter(Boolean).join(" and ")} stable,`:""} worth ${(G.expected.games||[0,0]).join("–")}% in games.</div>`
      :`<div class="job err"><span class="ph">GPU tuning isn't available on this PC</span><div class="msg">${esc((S.profile||{}).gpu_method||"Headroom tunes NVIDIA RTX cards through NVIDIA's driver API.")}</div></div>`; }
  if(!slidersInit){ slidersInit=true; $("sCore").max=S.limits.max_core; $("sMem").max=S.limits.max_mem;
    if(r){ $("sCore").value=r.core; $("sMem").value=r.mem_afterburner; } predict(); }
  $("swEff").checked=!!G.efficiency; $("swLogon").checked=!!G.logon;
  feed("gFeed", l=>/\[GPU|GPU|offset|Efficiency|logon|RESET/i.test(l.t));
}
function predict(){ const c=+$("sCore").value, m=+$("sMem").value; $("oCore").textContent=sg(c,0); $("oMem").textContent=sg(m,0);
  const p=predictGpu(c,m,S&&(S.gpu.baseline||{}).eff_clk); $("pBig").textContent=sg(p.g)+"%";
  $("pTxt").textContent=`about ${sg(p.c)}% clock under load, ${sg(p.m)}% memory bandwidth · games likely ${sg(p.lo)} … ${sg(p.hi)}%`; }
$("sCore").oninput=predict; $("sMem").oninput=predict;
$("gAuto").onclick=()=>startAuto(false); $("gQuick").onclick=()=>startAuto(true);
async function startAuto(quick){
  if(!await ask(quick?"Quick Auto OC":"GPU Auto OC","Close games and anything else using the GPU.\n\nThe screen may flicker, or the driver may reset during testing. That's expected and handled: Headroom goes back to stock and carries on.",{yes:"Start",icon:""})) return;
  act("/api/gpu/auto",{quick}); }
$("gStop").onclick=()=>act("/api/gpu/stop",{},"Stopping, returning to stock…");
$("gApply").onclick=async()=>{ const c=+$("sCore").value, m=+$("sMem").value, r=S.gpu.result;
  if(r&&(c>r.core||m>r.mem_afterburner)&&!await ask("Untested values","These are higher than the stable values Auto OC found (core "+sg(r.core,0)+", memory "+sg(r.mem_afterburner,0)+"). Apply anyway?",{yes:"Apply"})) return;
  act("/api/gpu/apply",{core:c,mem:m},"Applied"); };
$("gSaveP").onclick=async()=>{ const c=+$("sCore").value, m=+$("sMem").value;
  if(await ask("Save as profile","Make core "+sg(c,0)+" / memory "+sg(m,0)+" the saved profile? It's applied now and used by Apply at logon. Values above what Auto OC tested are refused.",{yes:"Save"})) act("/api/gpu/save_profile",{core:c,mem:m},"Saved profile updated"); };
$("gSaved").onclick=()=>act("/api/gpu/apply_saved",{},"Auto result applied");
$("gStock").onclick=()=>act("/api/gpu/stock",{},"GPU back to stock");
$("swEff").onchange=e=>act("/api/gpu/efficiency",{on:e.target.checked}).catch(()=>e.target.checked=!e.target.checked);
$("swLogon").onchange=e=>act("/api/gpu/logon",{on:e.target.checked}).catch(()=>e.target.checked=!e.target.checked);
$("resetAll").onclick=async()=>{ if(await ask("Reset to stock","The GPU goes back to stock clocks now and any running test stops.\n\nCPU settings are stored in the BIOS: set PBO and Curve Optimizer back to Auto there (or press F5 for defaults).",{yes:"Reset",danger:true,icon:""})) act("/api/reset_all",{},"Reset to stock"); };

// ---- fans tab
const FPRE={quiet:{core:[[50,0],[55,30],[65,42],[72,58],[78,80],[82,100]],mem:[[70,0],[76,35],[84,55],[90,75],[94,100]]},
  balanced:null, perf:{core:[[40,0],[42,40],[55,55],[65,72],[72,88],[78,100]],mem:[[55,0],[60,45],[72,65],[82,85],[90,100]]}};
let fc=null, fcDirty=false, fcSer="core", fcDrag=null;
const FX0=20,FX1=105,PL=44,PR=16,PT=14,PB=34,FW=640,FH=330;
const fx=t=>PL+(t-FX0)/(FX1-FX0)*(FW-PL-PR), fy=v=>PT+(1-v/100)*(FH-PT-PB);
const ix=x=>FX0+(x-PL)/(FW-PL-PR)*(FX1-FX0), iy=y=>100*(1-(y-PT)/(FH-PT-PB));
const clone=o=>JSON.parse(JSON.stringify(o));
function fInterp(pts,t){ if(t==null||!pts.length) return 0; if(t<pts[0][0]) return 0; if(t==pts[0][0]) return pts[0][1];
  for(let i=1;i<pts.length;i++) if(t<=pts[i][0]){ const [a,b]=pts[i-1],[c,d]=pts[i]; return b+(d-b)*(t-a)/Math.max(1,c-a); } return pts.at(-1)[1]; }
function fcWant(core,mj){ const E=S.fans.emergency; if(core==null) return [100,"core temp unreadable"];
  if(core>=E[0]||(mj!=null&&mj>=E[1])) return [100,"emergency"];
  return [Math.round(Math.max(fInterp(fc.core,core), mj!=null?fInterp(fc.mem,mj):fInterp(fc.core,core+8))),"curve"]; }
function drawFc(){
  const svg=$("fcSvg"), E=S.fans.emergency, g=S.sensors.gpu||{}, x=S.sensors.gpu_x||{};
  let h=`<rect class="emerg" x="${fx(E[0])}" y="${PT}" width="${fx(FX1)-fx(E[0])}" height="${FH-PT-PB}" style="opacity:${fcSer==="core"?1:.25}"/>`;
  for(let v=0;v<=100;v+=20) h+=`<line class="gl" x1="${PL}" x2="${FW-PR}" y1="${fy(v)}" y2="${fy(v)}"/><text class="ax" x="${PL-8}" y="${fy(v)+3}" text-anchor="end">${v}%</text>`;
  for(let t=30;t<=100;t+=10) h+=`<line class="gl" y1="${PT}" y2="${FH-PB}" x1="${fx(t)}" x2="${fx(t)}"/><text class="ax" x="${fx(t)}" y="${FH-PB+16}" text-anchor="middle">${t}°C</text>`;
  const ec=fcSer==="core"?E[0]:E[1];
  h+=`<line x1="${fx(ec)}" x2="${fx(ec)}" y1="${PT}" y2="${FH-PB}" stroke="var(--red)" stroke-dasharray="4 4" stroke-width="1.5"/><text class="ax" x="${fx(ec)+6}" y="${PT+38}" style="fill:var(--red)">100% from ${ec}°C</text>`;
  const col={core:css("--cyan"),mem:css("--purple")};
  for(const k of ["mem","core"].sort(a=>a===fcSer?1:-1)){
    const p=fc[k], on=k===fcSer, pts=[[FX0,0],[p[0][0],0],...p,[FX1,p.at(-1)[1]]];
    h+=`<polyline fill="none" stroke="${col[k]}" stroke-width="${on?2.5:1.5}" stroke-opacity="${on?1:.45}" stroke-linejoin="round" points="${pts.map(q=>fx(q[0])+","+fy(q[1])).join(" ")}"/>`;
    p.forEach((q,i)=>h+=`<circle class="pt ${on?"":"off"}" data-s="${k}" data-i="${i}" cx="${fx(q[0])}" cy="${fy(q[1])}" r="${on?6:4}" fill="${col[k]}"><title>${q[0]}°C → ${q[1]}%</title></circle>`);
  }
  const live=[[g.temp,col.core,"core"],[x.mem_junction,col.mem,"VRAM"]];
  for(const [t,c,n] of live) if(t!=null) h+=`<line x1="${fx(t)}" x2="${fx(t)}" y1="${PT}" y2="${FH-PB}" stroke="${c}" stroke-opacity=".6" stroke-width="1"/><text class="ax" x="${fx(t)+3}" y="${FH-PB-6}" style="fill:${c}">${n} ${Math.round(t)}°</text>`;
  if(g.fan!=null) h+=`<line x1="${PL}" x2="${FW-PR}" y1="${fy(g.fan)}" y2="${fy(g.fan)}" stroke="var(--claude)" stroke-dasharray="2 4" stroke-width="1.5"/><text class="ax" x="${FW-PR-2}" y="${fy(g.fan)-4}" text-anchor="end" style="fill:var(--claude)">fans ${g.fan}%</text>`;
  if(fcDrag){ const q=fc[fcDrag.s][fcDrag.i]; h+=`<text x="${fx(q[0])}" y="${fy(q[1])-12}" text-anchor="middle" style="fill:var(--text);font-size:12px;font-weight:600">${q[0]}°C · ${q[1]}%</text>`; }
  svg.innerHTML=h;
}
function renderFans(){
  const F=S.fans; if(!F) return;
  $("swFan").disabled=!S.caps.gpu_fans;
  if(!S.caps.gpu_fans&&!renderFans._n){ renderFans._n=1; $("fcNow").insertAdjacentHTML("beforebegin",`<div class="job err" style="margin-bottom:10px"><span class="ph">Fan control isn't available</span><div class="msg">Headroom's fan curve needs an NVIDIA card with controllable fans.</div></div>`); }
  if(!fcDirty||!fc) fc=clone({core:F.config.core,mem:F.config.mem});
  const st=F.status||{}, g=S.sensors.gpu||{}, x=S.sensors.gpu_x||{};
  const mode=!st.running?"card's own curve":st.mode==="yielded"?"paused: "+(st.yielded_to||"another program")+" controls the fans":st.why==="tuning boost"?"tuning boost":st.why==="emergency"?"EMERGENCY 100%":st.mode==="manual"?"Headroom curve":"curve: card in control (cool)";
  $("fcMode").textContent=mode; $("fcMode").className="tag "+(st.why==="emergency"?"bad":st.mode==="yielded"?"warn":"");
  const [want]=fcWant(g.temp,x.mem_junction), rpm=(x.fans||[]).filter(v=>v!=null);
  $("fcNow").innerHTML=stat("Fan speed",(g.fan??"–")+" %"+(rpm.length?" · "+rpm.map(v=>Math.round(v)).join(" / ")+" rpm":""))+
    stat("GPU core",fmt(g.temp)+" °C",lvl(g.temp,S.limits.gpu_core_warn,S.limits.gpu_core))+
    stat("VRAM (memory junction)",fmt(x.mem_junction)+" °C",lvl(x.mem_junction,S.limits.gpu_mem_warn,S.limits.gpu_mem))+
    stat("This curve wants",want?want+" %":"card decides (0%)")+(fcDirty?stat("Unsaved changes","yes","warn"):"");
  $("swFan").checked=!!F.config.enabled; $("swFanLogon").checked=!!F.logon; $("swFanLogon").disabled=!F.config.enabled;
  $("fcSave").disabled=!fcDirty&&F.config.enabled; $("fcUndo").disabled=!fcDirty;
  if(!fcDrag) drawFc();
}
function svgPt(e){ const r=$("fcSvg").getBoundingClientRect(); return [(e.clientX-r.left)*FW/r.width,(e.clientY-r.top)*FH/r.height]; }
function fcFix(k){ const p=fc[k]; p.sort((a,b)=>a[0]-b[0]); for(let i=1;i<p.length;i++) p[i][1]=Math.max(p[i][1],p[i-1][1]); }
$("fcSvg").addEventListener("pointerdown",e=>{ const c=e.target.closest(".pt"); if(!c||c.dataset.s!==fcSer||e.button!==0) return;
  fcDrag={s:c.dataset.s,i:+c.dataset.i}; $("fcSvg").setPointerCapture(e.pointerId); e.preventDefault(); });
$("fcSvg").addEventListener("pointermove",e=>{ if(!fcDrag) return; const [X,Y]=svgPt(e), p=fc[fcDrag.s], i=fcDrag.i;
  const lo=i?p[i-1][0]+1:FX0, hi=i<p.length-1?p[i+1][0]-1:FX1, vlo=i?p[i-1][1]:0, vhi=i<p.length-1?p[i+1][1]:100;
  p[i]=[Math.round(Math.max(lo,Math.min(hi,ix(X)))), Math.round(Math.max(vlo,Math.min(vhi,iy(Y))))]; fcDirty=true; drawFc(); });
const endDrag=()=>{ if(fcDrag){ fcDrag=null; renderFans(); } };
$("fcSvg").addEventListener("pointerup",endDrag); $("fcSvg").addEventListener("pointercancel",endDrag);
$("fcSvg").addEventListener("dblclick",e=>{ const [X,Y]=svgPt(e), t=Math.round(ix(X)); if(t<FX0||t>FX1) return;
  const p=fc[fcSer]; if(p.length>=10||p.some(q=>q[0]===t)) return; p.push([t,Math.max(0,Math.min(100,Math.round(iy(Y))))]); fcFix(fcSer); fcDirty=true; renderFans(); });
$("fcSvg").addEventListener("contextmenu",e=>{ e.preventDefault(); const c=e.target.closest(".pt"); if(!c||c.dataset.s!==fcSer) return;
  const p=fc[fcSer]; if(p.length<=2) return toast("A curve needs at least 2 points"); p.splice(+c.dataset.i,1); fcDirty=true; renderFans(); });
document.querySelectorAll("#fcSeg button").forEach(b=>b.onclick=()=>{ fcSer=b.dataset.s; document.querySelectorAll("#fcSeg button").forEach(x=>x.classList.toggle("sel",x===b)); drawFc(); });
document.querySelectorAll("[data-pre]").forEach(b=>b.onclick=()=>{ const P=FPRE[b.dataset.pre]||{core:[[45,0],[50,35],[60,50],[68,65],[75,80],[80,100]],mem:[[62,0],[70,40],[80,60],[88,80],[94,100]]};
  fc=clone(P); fcDirty=true; renderFans(); toast(b.textContent+" preset loaded. Save to use it."); });
$("fcUndo").onclick=()=>{ fcDirty=false; renderFans(); };
$("fcSave").onclick=async()=>{ await act("/api/fans/set",{enabled:true,core:fc.core,mem:fc.mem},"Fan curve saved and on"); fcDirty=false; };
$("swFan").onchange=async e=>{ const on=e.target.checked;
  try{ await act("/api/fans/set",on&&fcDirty?{enabled:true,core:fc.core,mem:fc.mem}:{enabled:on},on?"Fan curve on":"Fans back to the card's own curve"); if(on) fcDirty=false; }catch(_){ e.target.checked=!on; } };
$("swFanLogon").onchange=e=>act("/api/fans/logon",{on:e.target.checked},e.target.checked?"Fan curve starts at logon":"Removed from logon").catch(()=>e.target.checked=!e.target.checked);

// ---- setup tab
let SU=null, suT=0, suWasBusy=false, suFirstShown=false;
async function renderSetup(force){
  const st=S.setup||{};
  if(st.needed&&!suFirstShown){ suFirstShown=true; tab("setup"); }
  $("suFirst").style.display=st.needed?"":"none";
  const vis=document.querySelector("#v-setup.sel");
  if(!vis&&!force) return;
  if(force||!SU||Date.now()-suT>4000||(suWasBusy&&!st.busy)){ suT=Date.now(); try{ SU=await api("/api/setup"); }catch(e){ return; } }
  if(suWasBusy&&!st.busy){ profiles=null; $("profBody")._done=0; }
  suWasBusy=!!st.busy;
  const sc=SU.scan||{}, c=sc.cpu||{}, b=sc.board||{}, g=sc.gpu||{}, nv=g.nvidia||{}, m=sc.memory||{}, cap=sc.caps||{}, sn=sc.sensors||{};
  $("suWhen").textContent=sc.when?"scanned "+sc.when:"not scanned yet";
  $("suHw").innerHTML=sc.when?stat("CPU",esc(c.name))+stat("Motherboard",esc(`${b.maker||""} ${b.model||""} · BIOS ${b.bios||"?"}`))+
    stat("GPU",esc(g.name||"none found")+(g.board_vendor?` <span class="hint">(${esc(g.board_vendor)})</span>`:""))+
    (nv.present?stat("GPU driver · power limit",`${esc(nv.driver)} · ${nv.power?`${nv.power.default_w} W (max ${nv.power.max_w} W)`:"?"}`):"")+
    stat("Memory",esc(`${m.total_gb} GB DDR${m.ddr||"?"} at ${m.running_mts} MT/s · `+(m.kits||[]).map(k=>`${k.count}x ${k.part}`).join(" + ")))+
    stat("Windows",esc((sc.os||{}).name||"")):`<div class="hint">${st.busy?"Scanning…":"Press Re-scan."}</div>`;
  const C=(ok,t,sub)=>`<div class="cap"><i class="ic ${ok?"good":"warn"}">${ok?"&#xE73E;":"&#xE7BA;"}</i><div>${t}<small>${sub}</small></div></div>`;
  $("suCaps").innerHTML=sc.when?C(cap.gpu_tuning,"GPU auto overclock",cap.gpu_tuning?"NVIDIA clock offsets are available on this card and driver.":"Needs an NVIDIA RTX card with a recent driver.")+
    C(cap.gpu_fans,"GPU fan curve",cap.gpu_fans?`${nv.fans} fan${nv.fans>1?"s":""} controllable.`:"Needs an NVIDIA card with controllable fans.")+
    C(cap.cpu_co,"CPU Curve Optimizer search",cap.cpu_co?"Ryzen with Curve Optimizer: CoreCycler finds per-core values.":"Only on AMD Ryzen 5000 / 7000 / 9000.")+
    C(cap.cpu_temps,"CPU temperature",cap.cpu_temps?`Reading ${fmt(sn.cpu_temp,1)} °C.`:"Needs the sensor app (below).")+
    C(cap.vram_temp,"GPU memory temperature",cap.vram_temp?`Reading ${fmt(sn.gpu_mem_junction)} °C.`:"Needs the sensor app; the fan curve then uses the core temperature only.")+
    C(cap.pawnio,"PawnIO driver",cap.pawnio?"Installed (used by the sensor app and CoreCycler).":`Not installed. CPU sensors and CoreCycler need it: <a href="https://pawnio.eu" target="_blank">pawnio.eu</a>`)+
    (cap.laptop?C(false,"Laptop","Laptops have tight cooling and locked limits; Headroom is built for desktops."):""):"";
  const src=SU.profile_sources||{}, R={research:"Researched with Claude",builtin:"Built-in research",family:"Generic for its family"};
  $("suSrc").innerHTML=Object.keys(src).length?["gpu","cpu","board"].map(k=>stat(k.toUpperCase(),`<span class="chip ${src[k]}">${R[src[k]]||src[k]||"–"}</span>`)).join("")+
    (SU.research?`<div class="hint">Research ${esc(SU.research.when)} · ${SU.research.sources.length} sources (see Profiles).</div>`:
     Object.values(src).includes("family")?`<div class="hint">Generic means no profile for the exact part ships with Headroom: Research fills it in with real results for your model.</div>`:""):`<div class="hint">No profile yet.</div>`;
  $("suKeyState").innerHTML=SU.has_key?`<span class="good">A key is saved</span> (encrypted for this Windows user). Paste a new one to replace it, or save an empty box to remove it.`:"Get a key at console.anthropic.com → API keys.";
  $("suResearch").disabled=!!st.busy||!SU.has_key||!sc.when; $("suRebuild").disabled=!!st.busy||!sc.when; $("suScan").disabled=!!st.busy;
  const job=$("suJob"); job.className="job "+(st.busy?"":st.error?"err":"idle"); $("suPh").textContent=st.busy?st.busy:st.error?"Failed":"Idle";
  $("suMsg").textContent=st.busy?(st.progress||""):st.error||(SU.research?"Last research: "+SU.research.when:"");
  $("suLhm").innerHTML=SU.lhm?stat("LibreHardwareMonitor","running, web server on :8085","good"):
    stat("LibreHardwareMonitor","not answering on :8085","warn")+`<p class="txt">Headroom reads CPU temperature, GPU memory temperature and fan speeds from LibreHardwareMonitor (free, open source). ${SU.lhm_installed?"It's installed but not running.":"It isn't installed."}</p>
     <button class="btn q" id="suLhmBtn" ${st.busy?"disabled":""}><i class="ic">&#xE896;</i>${SU.lhm_installed?"Start sensors":"Install sensors (from GitHub)"}</button>`;
  if($("suLhmBtn")) $("suLhmBtn").onclick=()=>act("/api/setup/lhm",{},"Setting up sensors…");
  const fans=Object.entries(sn.fans||(S.sensors.fans||{})), cool=SU.cooling||{};
  const opts=(sel)=>`<option value="">not watched</option>`+fans.map(([k,v])=>`<option value="${esc(k)}" ${k===sel?"selected":""}>${esc(k)} · ${fmt((S.sensors.fans||{})[k]??v)} rpm</option>`).join("");
  if(document.activeElement!==$("suPump")) $("suPump").innerHTML=opts(cool.pump_sensor);
  if(document.activeElement!==$("suRad")) $("suRad").innerHTML=opts(cool.radiator_sensor);
  if(document.activeElement!==$("suPumpMin")) $("suPumpMin").value=cool.pump_min_rpm||1000;
}
$("suScan").onclick=()=>act("/api/setup/scan",{},"Scanning…").then(()=>renderSetup(true));
$("suRebuild").onclick=()=>act("/api/setup/rebuild",{},"Rebuilding the profile from built-in data").then(()=>renderSetup(true));
$("suKeySave").onclick=async()=>{ await act("/api/setup/key",{key:$("suKey").value.trim()},$("suKey").value.trim()?"Key saved":"Key removed"); $("suKey").value=""; renderSetup(true); };
$("suResearch").onclick=async()=>{ if(await ask("Research with Claude","Claude will search the web for results on your exact parts and build a profile with sources. It takes a few minutes and is billed to your Anthropic API account.\n\nOnly hardware model names, versions and limits are sent.",{yes:"Start",icon:""})) act("/api/setup/research",{}).then(()=>renderSetup(true)); };
$("suCoolSave").onclick=()=>act("/api/setup/cooling",{pump_sensor:$("suPump").value,radiator_sensor:$("suRad").value,pump_min_rpm:+$("suPumpMin").value},"Cooling check saved").then(()=>renderSetup(true));

// ---- CPU tab
const PH=[["baseline","Baseline"],["bios1","BIOS: PBO"],["search","Per-core search"],["bios2","BIOS: CO values"],["verify","Verify"],["done","Done"]];
let cpuWaitBios=false;
function renderCpu(){
  const C=S.cpu, i=PH.findIndex(p=>p[0]===C.phase);
  $("cSteps").innerHTML=PH.map((p,k)=>`<div class="step ${k<i?"done":k===i?"cur":""}"><i>${k<i?"✓":k+1}</i>${p[1]}</div>`).join("");
  $("cWatch").textContent=C.watchdog?"thermal watchdog armed":C.busy?C.busy+"…":"";
  if(document.activeElement!==$("cStart")) $("cStart").value=String(C.co_start);
  let h="", btn="", b=C.baseline||{};
  if(C.phase==="baseline"){
    h=`<p class="txt">Measures the CPU as it is now, so keep the BIOS unchanged for this step. Pre-flight checks admin rights, the PawnIO driver, LibreHardwareMonitor and the pump / CPU fan rpm you picked in Setup.</p>
      <div class="ml"><span>CoreCycler</span><b class="${C.corecycler?"good":"warn"}">${C.corecycler||"not installed yet: press Install CoreCycler"}</b></div>`;
    btn=" Run pre-flight + baseline"; }
  else if(C.phase==="bios1"){
    h=`<div class="ml"><span>Baseline</span><b>single ${fmt(b.single)} · multi ${fmt(b.multi)} · peak ${fmt(b.max_clock)} MHz · ${fmt(b.package_w)} W · ${fmt(b.tdie,1)} °C</b></div>
      ${b.stock_like===false?'<div class="hint warn">The PBO boost was already active, so the uplift is measured against that, not stock.</div>':""}
      <pre class="sheet">${esc(C.sheet)}</pre><p class="txt">After rebooting, open Headroom and press the button. It checks that the CPU now boosts above its stock maximum.</p>`;
    btn=" I've done the BIOS step · check it"; }
  else if(C.phase==="search"){
    const s=C.search||{}, v=s.values||[];
    h=`<p class="txt">CoreCycler is <b class="${s.running?"good":"warn"}">${s.running?"running":"not running"}</b>. It tests one core at a time with y-cruncher, about 20 min per core per pass, 3 passes. Each error raises that core's value by 1. Leave the PC alone; if it freezes, press reset.</p>
      <div class="cores">${v.map((x,k)=>`<div class="core ${x>C.co_start?"adj":""}"><small>CORE ${k}</small><b>${x}</b></div>`).join("")}</div>
      <div class="ml"><span>Adjustments</span><b>${s.adjustments??0}</b><span>WHEA errors</span><b class="${s.whea?"warn":""}">${s.whea??0}</b></div>
      <p class="hint">When CoreCycler has finished (or after at least one full pass), close its window with Ctrl+C and press the button.</p>`;
    btn=" Search finished · make BIOS sheet"; }
  else if(C.phase==="bios2"){
    const p=C.predict||{};
    h=`<div class="cores">${(C.final||[]).map((x,k)=>`<div class="core"><small>CORE ${k}</small><b>${x}</b></div>`).join("")}</div>
      <div class="hint">Found ${JSON.stringify(C.found)} and added a +3 safety margin.</div><pre class="sheet">${esc(C.sheet)}</pre>
      <div class="ml"><span>Predicted</span><b>single ${sg(p.single_thread_pct)}% · multi ${sg(p.multi_thread_pct)}% · CPU-bound games ${sg(p.estimated_cpu_bound_games_pct)}%</b></div>`;
    btn=" Values are in BIOS · verify"; }
  else if(C.phase==="verify"){
    h=`<p class="txt">CoreCycler is running an all-core check at your BIOS values: 2 minutes per core, 2 threads, no auto-adjust. When it's done, press the button: Headroom benchmarks again and works out the uplift.</p>
      <p class="hint">If a core fails, raise it by 2 in BIOS (e.g. −24 → −22) and verify again.</p>`;
    btn=" Verification finished · measure"; }
  else { const r=C.result||{}, u=r.uplift||{}, m=u.measured||{};
    h=`<div class="up" style="grid-template-columns:auto 1fr;align-items:center"><div><div class="big">${sg(u.estimated_cpu_bound_games_pct)}%</div><div class="hint" style="margin:0">CPU-bound games</div></div>
      <div class="ml" style="margin:0"><span>Curve Optimizer</span><b>${JSON.stringify(r.final)} · +200 MHz · 85 °C limit</b>
      <span>Single / multi</span><b>${sg(m.single_thread_pct)}% / ${sg(m.multi_thread_pct)}%</b>
      <span>Peak clock</span><b>${(m.max_clock||[]).map(v=>fmt(v)).join(" → ")} MHz</b>
      <span>Package power</span><b>${(m.package_w||[]).map(v=>fmt(v)).join(" → ")} W</b>
      <span>Peak temp</span><b>${(m.tdie_c||[]).map(v=>fmt(v,1)).join(" → ")} °C</b>
      <span>Verify errors</span><b>${r.verify_errors&&Object.keys(r.verify_errors).length?JSON.stringify(r.verify_errors):"none"}</b></div></div>`;
    btn=" Re-measure"; }
  if(C.trip) h=`<div class="job err"><span class="ph">Watchdog stopped the test</span><div class="msg">${esc(C.trip)}</div></div>`+h;
  $("cBody").innerHTML=h;
  $("cAct").innerHTML=`<i class="ic">${btn[0]}</i>${btn.slice(2)}`; $("cAct").disabled=!!C.busy;
  $("cInstall").style.display=C.corecycler?"none":""; $("cInstall").disabled=!!C.busy;
  const e=C.expected||{}, P=S.profile||{}, ct=P.cpu_typical||{}, bo=ct.boost_override_mhz||0, coStart=Math.max(-50,Math.min(0,parseInt($("cStart").value,10)||0)), pr=predictCpu(bo,coStart), co=ct.co_per_core;
  $("cExp").innerHTML=`<div class="sec"><span class="label"><i class="ic">&#xE9D2;</i>What to expect · ${esc(P.cpu_label||S.hw.cpu)}</span></div>
    ${P.cpu_advisory?`<div class="hint ${esc(P.cpu_advisory.level)}" style="font-size:13px">${esc(P.cpu_advisory.text)}</div>`:""}
    <p class="txt">${esc(P.cpu_method||"")}${co&&co[1]?` Typical chips run Curve Optimizer at ${co[0]} to ${co[1]} per core.`:""}</p>
    <div class="ml"><span>Single-thread</span><b>+${(e.single_thread||[0,0]).join("–")}%</b><span>Multi-thread</span><b>+${(e.multi_thread||[0,0]).join("–")}%</b>
      <span>CPU-bound games</span><b>+${(e.games_cpu_bound||[0,0]).join("–")}%</b><span>1440p / 4K games</span><b>+${(e.games_gpu_bound||[0,1]).join("–")}%</b></div>
    <div class="upc" style="margin-top:14px"><div class="label">Headroom's prediction · ${bo?"+"+esc(bo)+" / ":""}CO ${coStart}</div><div class="big" style="font-size:26px">${sg(pr.cb)}%</div>
      <div class="hint" style="margin:0">CPU-bound games · single ${sg(pr.st)}% · multi ${sg(pr.mt)}%</div></div>
    <p class="txt" style="margin-top:14px">The real wins from Curve Optimizer are lower voltage, a cooler CPU and quieter fans, for the same or slightly better speed.</p>
    <p class="hint">Ryzen often freezes instead of rebooting on a bad value. Just press reset: CoreCycler and Headroom pick up again after you log in.</p>`;
  if(!S.caps.cpu_co) $("cBody").innerHTML=`<div class="job err"><span class="ph">Automatic CPU tuning isn't available on this CPU</span><div class="msg">${esc(P.cpu_method||"Headroom's CPU tuning uses AMD Curve Optimizer (Ryzen 5000/7000/9000).")}</div></div>`+$("cBody").innerHTML;
  $("cAct").disabled=$("cAct").disabled||!S.caps.cpu_co;
  feed("cFeed", l=>/CPU|CoreCycler|PawnIO|bench/i.test(l.t));
  if(cpuWaitBios&&!C.busy){ cpuWaitBios=false; bios1Next(); }
}
$("cStart").onchange=()=>renderCpu();
$("cAct").onclick=async()=>{
  const ph=S.cpu.phase;
  if(ph==="baseline"||ph==="bios1"||ph==="bios2"){ const pf=await api("/api/cpu/preflight",{}); const probs=pf.result||[];
    if(probs.length){ await ask("Pre-flight","• "+probs.join("\n\n• "),{no:"",icon:""}); return; } }
  if(ph==="search"&&S.cpu.search&&S.cpu.search.running&&!await ask("CoreCycler still running","CoreCycler still looks like it's running. Use the current values anyway?",{yes:"Use them"})) return;
  await act("/api/cpu/action",{});
  if(ph==="bios1") cpuWaitBios=true;
};
async function bios1Next(){
  const r=await api("/api/status?since="+seq), ok=r.cpu.bios1_ok;
  const msg=ok?`The boost override is active (peak ${fmt(r.cpu.bios1_clock)} MHz).\n\nStart CoreCycler's per-core search from CO ${parseInt($("cStart").value,10)||0}? It runs for hours, so leave the PC alone. If it freezes, press reset.`
            :`The CPU only reached ${fmt(r.cpu.bios1_clock)} MHz, so the BIOS change may not have saved.\n\nStart the CO search anyway?`;
  if(await ask(ok?"Start the CO search":"Boost override not detected",msg,{yes:"Start search"})) act("/api/cpu/start_search",{start:+$("cStart").value},"CoreCycler started");
}
$("cInstall").onclick=()=>act("/api/cpu/install",{},"Downloading CoreCycler…");
$("cReset").onclick=async()=>{ if(await ask("Start over","Forget all CPU tuning progress and go back to step 1?",{yes:"Start over",danger:true})) act("/api/cpu/reset",{}); };

// ---- feeds
function feed(id,filter){ const el=$(id), want=logs.filter(filter).slice(-120);
  if(el._n===want.length&&el._last===(want.at(-1)||{}).id) return; el._n=want.length; el._last=(want.at(-1)||{}).id;
  const atEnd=el.scrollTop+el.clientHeight>=el.scrollHeight-20;
  el.innerHTML=want.map(l=>`<div class="${l.lvl}">${esc(l.t.slice(11))}</div>`).join("")||'<div style="color:var(--faint)">Nothing yet.</div>';
  if(atEnd) el.scrollTop=el.scrollHeight; }
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl=u=>/^https:\/\/[^\s"'<>\\`]+$/i.test(String(u||""))?String(u):"";   // only plain https links from profiles

// ---- bench
function renderBench(){ const rows=S.bench;
  $("bRows").innerHTML=rows.map((r,k)=>`<tr><td>${esc(r[0])}</td><td class="n">${r[1]}</td><td class="n">${r[2]}</td><td class="n"><b class="${pct(r[2],r[1])>=0?"good":"bad"}">${sg(pct(r[2],r[1]))}%</b></td><td style="width:30px"><button class="hb" data-del="${k}" title="Remove"><i class="ic">&#xE74D;</i></button></td></tr>`).join("")+
    (rows.length?`<tr><td><b>Average</b></td><td></td><td></td><td class="n"><b>${sg(rows.reduce((s,r)=>s+pct(r[2],r[1]),0)/rows.length)}%</b></td><td></td></tr>`:`<tr><td colspan="5" class="hint">No scores yet.</td></tr>`);
  document.querySelectorAll("[data-del]").forEach(b=>b.onclick=()=>{ const r=S.bench.slice(); r.splice(+b.dataset.del,1); act("/api/bench",{rows:r}); }); }
$("bAdd").onclick=()=>{ const s=parseFloat($("bS").value), t=parseFloat($("bT").value); if(isNaN(s)||isNaN(t)) return toast("Enter both scores");
  act("/api/bench",{rows:[...S.bench,[$("bN").value||"Test",s,t]]}); $("bN").value=$("bS").value=$("bT").value=""; };
$("bClr").onclick=()=>act("/api/bench",{rows:[]});

// ---- profiles
async function renderProfiles(){
  if(!profiles) profiles=await api("/api/profiles"); if($("profBody")._done) return; $("profBody")._done=1;
  const H={cpu:"",gpu:"",board:"",memory:"",cooler:""};
  const src=profiles._sources||{}, R={research:"researched with Claude",builtin:"built-in research",family:"generic for the family"};
  let h=`<div class="sec"><span class="label"><i class="ic">&#xE8F1;</i>Active profile for this PC</span><span class="hint" style="margin:0">${esc(profiles._about||"")}</span></div>`;
  if(!Object.keys(profiles).filter(k=>!k.startsWith("_")).length) h+=`<div class="hint">No profile yet: finish Setup first.</div>`;
  for(const [sec,items] of Object.entries(profiles)){ if(sec.startsWith("_")) continue;
    for(const [name,p] of Object.entries(items)){
      h+=`<div class="prof"><div class="paneh" style="margin:0"><div class="tile"><i class="ic">${H[sec]||""}</i></div><div><h3>${esc(name)}</h3><small>${sec.toUpperCase()}${src[sec]?` · <span class="chip ${src[sec]}">${R[src[sec]]||src[sec]}</span>`:""}</small></div></div><div class="kv">`;
      for(const [k,v] of Object.entries(p)){ if(["community_results","sources","safety","settings_to_enter","_source","label","rejected","advisory"].includes(k)||v==null||v==="") continue;
        h+=`<span>${k.replace(/_/g," ")}</span><div>${typeof v==="object"?esc(Object.entries(v).map(([a,b])=>a.replace(/_/g," ")+": "+(Array.isArray(b)?b.join(" – "):b)).join(" · ")):esc(String(v))}</div>`; }
      h+=`</div>`;
      if(p.settings_to_enter) h+=`<table style="margin-top:10px"><thead><tr><th>BIOS setting</th><th>Value</th></tr></thead><tbody>${p.settings_to_enter.map(r=>`<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`).join("")}</tbody></table>`;
      if(p.rejected&&p.rejected.length) h+=`<div class="label" style="margin-top:12px">Left off the BIOS sheet</div><div class="hint">Research suggested these, but they are outside what Headroom considers safe (voltages, power limits, memory, unknown settings), so they are not shown as instructions.</div><table><tbody>${p.rejected.map(r=>`<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td class="hint">${esc(r[2])}</td></tr>`).join("")}</tbody></table>`;
      if(p.advisory) h+=`<div class="hint ${esc(p.advisory.level)}" style="margin-top:10px">${esc(p.advisory.text)}</div>`;
      if(p.community_results) h+=`<table style="margin-top:12px"><thead><tr><th>Source</th><th>Settings</th><th>Result</th></tr></thead><tbody>${p.community_results.map(c=>`<tr><td><b>${safeUrl(c.url)?`<a href="${esc(safeUrl(c.url))}" target="_blank" rel="noopener">${esc(c.source)}</a>`:esc(c.source)}</b></td><td>${esc(c.settings)}</td><td>${esc(c.result)}</td></tr>`).join("")}</tbody></table>`;
      if(p.safety){ const sf=p.safety; h+=`<div class="label" style="margin-top:12px">Safety</div><ul>${(sf.notes||[]).map(n=>`<li>${esc(n)}</li>`).join("")}</ul>`; }
      if(p.sources&&p.sources.length) h+=`<div class="label" style="margin-top:12px">Sources</div><ul>${p.sources.filter(safeUrl).map(s=>`<li><a href="${esc(s)}" target="_blank" rel="noopener">${esc(s.replace(/^https?:\/\/(www\.)?/,"").slice(0,90))}</a></li>`).join("")}</ul>`;
      h+=`</div>`; } }
  $("profBody").innerHTML=h;
}

// ---- crash watch, re-check, per-game apply (GPU tab)
const KIND={gpu_tdr:"GPU driver reset",gpu_app_crash:"Game crash in the NVIDIA driver",whea_corrected:"CPU hardware error (corrected)",
  whea_fatal:"CPU hardware error",unexpected_shutdown:"Unexpected shutdown",bugcheck:"Bluescreen"};
let stInit=false, gaInit=false;
function renderStability(){
  const T=S.stability||{}, C=T.config||{}, rc=S.recheck, rv=S.reverted;
  $("swCrash").checked=!!T.tasks_installed;
  if(!stInit&&T.config){ stInit=true; $("stAuto").checked=!!C.auto_backoff; $("stMax").value=C.max_crashes; $("stDays").value=C.window_days; }
  const n=Object.values(T.counts||{}).reduce((a,b)=>a+b,0);
  $("gStabTag").textContent=T.tasks_installed?(n?n+" in "+C.window_days+" days":"watching"):"off"; $("gStabTag").className="tag "+(n?"warn":"");
  const cores=Object.entries(T.cpu_cores_to_loosen||{});
  $("gStab").innerHTML=`<div class="hint">${esc(T.status||"")}</div>`+
    (cores.length?`<div class="hint warn">CPU errors point at core${cores.length>1?"s":""} ${cores.map(([c,k])=>esc(c)+" ("+k+"x)").join(", ")}: raise their Curve Optimizer value by 2 in the BIOS (e.g. -20 → -18).</div>`:"")+
    ((T.recent||[]).length?`<table style="margin-top:8px"><thead><tr><th>When</th><th>What</th><th>Action</th></tr></thead><tbody>${T.recent.slice(0,6).map(e=>
      `<tr><td>${esc(new Date(e.t*1000).toLocaleString())}</td><td>${esc(KIND[e.kind]||e.kind)}${e.bugcheck?` <span class="hint">(${esc(e.bugcheck)})</span>`:""}</td><td>${esc(e.action||"")}</td></tr>`).join("")}</tbody></table>`:"");
  const busy=S.gpu.busy;
  $("gRecheck").innerHTML=rv?`<div class="job err" style="margin-bottom:12px"><span class="ph">Overclock turned off after repeated crashes</span><div class="msg">${esc(rv.reason||"")} (${esc(rv.when||"")}). Run Auto OC again to find new stable settings.</div></div>`
    :rc&&rc.due?`<div class="job" style="margin-bottom:12px"><span class="ph">Re-check recommended</span><div class="msg">${esc(rc.reason||"")}</div><div class="row" style="margin-top:8px"><button class="btn pri" id="gRecheckBtn" ${busy?"disabled":""}>Quick re-check (~5 min)</button></div></div>`:"";
  if($("gRecheckBtn")) $("gRecheckBtn").onclick=()=>act("/api/gpu/recheck",{},"Re-checking the saved result…");
  const M=(S.games||{}).mode||{};
  if(!gaInit&&M.mode){ gaInit=true; $("gaMode").value=M.mode; $("gaGames").value=(M.games||[]).join(", "); }
}
$("swCrash").onchange=e=>act("/api/stability/watch",{on:e.target.checked},e.target.checked?"Crash watch on":"Crash watch off").catch(()=>e.target.checked=!e.target.checked);
$("stSave").onclick=()=>act("/api/stability/config",{auto_backoff:$("stAuto").checked,max_crashes:+$("stMax").value,window_days:+$("stDays").value},"Saved");
$("stCheck").onclick=async()=>{ const r=await act("/api/stability/check",{}); toast(r.new?r.new+" new event(s) recorded":"No new crashes"); };
$("gaSave").onclick=()=>act("/api/games/apply_mode",{mode:$("gaMode").value,games:$("gaGames").value.split(/[,\s]+/).filter(Boolean)},"Saved");

// ---- game measurement (Benchmarks tab)
async function gmList(){ try{ const r=await api("/api/games/processes",{}); const cur=$("gmProc").value;
  $("gmProc").innerHTML=`<option value="">Pick the running game…</option>`+r.processes.map(p=>`<option ${p===cur?"selected":""}>${esc(p)}</option>`).join(""); }catch(e){ toast(e.message); } }
$("gmRefresh").onclick=gmList;
$("gmStart").onclick=()=>act("/api/games/measure",{process:$("gmProc").value,seconds:+$("gmSecs").value,rounds:+$("gmRounds").value},"Measuring: keep playing a steady scene");
$("gmStop").onclick=()=>act("/api/games/stop",{},"Stopping");
function renderGames(){
  const G=S.games||{}, pr=G.progress||{};
  $("gmMsg").textContent=pr.msg||""; $("gmMsg").className="hint"+(pr.error?" bad":"");
  $("gmStart").disabled=!!pr.busy||!S.gpu.result; $("gmStop").disabled=!pr.busy;
  const rs=G.results||[];
  $("gmRes").innerHTML=rs.length?`<table style="margin-top:10px"><thead><tr><th>Game</th><th style="text-align:right">Stock FPS</th><th style="text-align:right">Tuned FPS</th><th style="text-align:right">Avg</th><th style="text-align:right">1% low</th><th>Verdict</th></tr></thead><tbody>${rs.map(r=>
    `<tr><td>${esc(r.process)}<div class="hint" style="margin:0">${esc(r.when)}</div></td><td class="n">${fmt(r.stock.avg_fps,1)}</td><td class="n">${fmt(r.tuned.avg_fps,1)}</td>
     <td class="n"><b class="${r.uplift_pct.avg_fps>=0?"good":"bad"}">${sg(r.uplift_pct.avg_fps)}%</b></td><td class="n">${sg(r.uplift_pct.low1)}%</td><td>${esc(r.verdict)}</td></tr>`).join("")}</tbody></table>`:"";
}

// ---- research settings + updates (Setup tab)
let resInit=false;
function renderResearch(){
  const R=S.research||{}, E=R.estimate||{}, cli=R.cli||{};
  if(!resInit&&R.research_backend){ resInit=true; $("suBackend").value=R.research_backend; $("suCap").value=R.research_cap_usd_month; }
  const api_=$("suBackend").value==="api";
  $("suKey").parentElement.style.display=api_?"":"none"; $("suKeyState").style.display=api_?"":"none";
  $("suResInfo").textContent=api_?`About $${fmt(E.typical_usd,2)} per run (at most $${fmt(E.max_usd,2)}), billed to your API key. This month: $${fmt(R.research_spend_month_usd,2)}${R.research_cap_usd_month?` of your $${fmt(R.research_cap_usd_month,0)} cap`:""}.`
    :(cli.installed?"Uses Claude Code at "+(cli.path||"")+". ":"Claude Code isn't installed (claude.ai/code). ")+(cli.note||"");
  const busy=(S.setup||{}).busy; $("suCancel").style.display=busy&&/Research/i.test(busy)?"":"none";
  const U=S.update||{}; $("upVer").textContent="v"+(S.version||"?");
  if(U.error!==undefined||U.latest){ $("upMsg").textContent=U.error?U.error:U.available?`Version ${U.latest} is available. ${U.notes||""}`:`Headroom is up to date (${U.current}).`; }
  $("upInstall").style.display=U.available?"":"none";
}
$("suResSave").onclick=()=>act("/api/research/settings",{backend:$("suBackend").value,cap:+$("suCap").value},"Saved");
$("suBackend").onchange=renderResearch;
$("suCancel").onclick=()=>act("/api/setup/research_cancel",{},"Cancelling research…");
$("upCheck").onclick=()=>act("/api/update/check",{},"");
$("upInstall").onclick=async()=>{ if(await ask("Install update","Headroom downloads the new installer, checks it against the signed release and starts it. Headroom closes during the install.",{yes:"Install"})) act("/api/update/install",{},"Starting the installer…"); };

// ---- poll
async function poll(){
  try{
    const r=await api("/api/status?since="+seq); S=r; seq=r.seq; logs=logs.concat(r.logs).slice(-400);
    rings(); renderDash(); renderGpu(); renderFans(); renderCpu(); renderBench(); renderSetup(); renderStability(); renderGames(); renderResearch();
    feed("lFeed",()=>true); feed("dFeed",()=>true);
    const last=logs.at(-1); $("navInfo").textContent=last?last.t.slice(11):"";
    const ap=S.gpu.applied, pg=$("pGpu");
    pg.className="pill "+(S.gpu.busy?"busy":ap?"on":""); pg.querySelector("span").textContent=S.gpu.busy?"GPU tuning":ap?`GPU ${sg(ap.core,0)} / ${sg(ap.mem*S.gpu.mem_ratio,0)}`:"GPU stock";
    const pc=$("pCpu"); pc.className="pill "+(S.cpu.trip?"bad":S.cpu.busy||S.cpu.watchdog?"busy":S.cpu.phase==="done"?"on":"");
    pc.querySelector("span").textContent="CPU · "+(S.cpu.busy||({baseline:"stock",bios1:"BIOS step",search:"searching",bios2:"BIOS step",verify:"verifying",done:"tuned"}[S.cpu.phase]));
    if(!S.admin&&!poll._warned){ poll._warned=1; toast("Read-only: start Headroom from its launcher so it runs as administrator.",7000); }
  }catch(e){ }
}
setTheme(location.hash==="#light"?"light":location.hash==="#dark"?"dark":(localStorage.getItem("theme")||"dark"));
tab((location.hash.match(/tab=(\w+)/)||[])[1]||localStorage.getItem("tab")||"dash");
poll(); setInterval(poll,1000);

// Native window host (Headroom.exe): header is the title bar, edges resize, buttons drive the window.
(function(){
  const wv=window.chrome&&window.chrome.webview; if(!wv) return;
  const root=document.documentElement; root.classList.add("native");
  const post=m=>wv.postMessage(m), hdr=document.querySelector("header");
  const interactive="button,input,select,textarea,a,label,.tabs,.pill,[data-tab],[data-w]";
  hdr.addEventListener("mousedown",e=>{ if(e.button===0&&e.detail===1&&!e.target.closest(interactive)) post("drag"); });
  hdr.addEventListener("dblclick",e=>{ if(!e.target.closest(interactive)) post("max"); });
  document.querySelectorAll("[data-w]").forEach(b=>b.addEventListener("click",async()=>{
    if(b.dataset.w==="close"){ try{ const c=await api("/api/can_close",{});
      if(!c.ok&&!await ask("GPU tuning is running","Closing stops the tuning run and returns the GPU to stock.",{yes:"Stop and close",danger:true})) return;
      if(c.cpu_watch&&!await ask("CPU test running","Closing Headroom turns off the thermal watchdog. The BIOS 85 °C limit still protects the CPU.",{yes:"Close anyway",danger:true})) return; }catch(e){} }
    post(b.dataset.w); }));
  for(const d of ["n","s","e","w","nw","ne","sw","se"]){ const el=document.createElement("div"); el.className="edge e-"+d;
    el.addEventListener("mousedown",e=>{ if(e.button===0){ e.preventDefault(); post("size:"+d); } }); document.body.appendChild(el); }
  wv.addEventListener("message",e=>{ const m=e.data==="maxed"; root.classList.toggle("maxed",m);
    document.getElementById("maxIc").innerHTML=m?"&#xE923;":"&#xE922;"; document.querySelector('[data-w=max]').title=m?"Restore":"Maximise"; });
})();
