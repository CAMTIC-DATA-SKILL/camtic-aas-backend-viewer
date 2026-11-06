/* AAS path-qualified, quality-filtered six-axis robot mapping.
   AAS frame.cartesian is intentionally NOT trusted: upstream frame_from_mapping
   buckets every property named X/Y/Z/Rx/Ry/Rz, overwriting earlier groups. */
export const LABELS=['X','Y','Z','Rx','Ry','Rz','J1','J2','J3','J4','J5','J6'];
const numeric = v => (typeof v === 'number' && Number.isFinite(v)) ? v : ((typeof v==='string' && v.trim()!=='' && Number.isFinite(Number(v))) ? Number(v) : null);
const norm = s => String(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');
const PARTS = /[.\/[\]_-]+/;
const BAD_CONTEXT=/target|command|desired|request|planned|program|reference|nominal|setpoint|limit|velocity|speed|accel|torque|power|offset|temperature|force|counter|alarm|error|frameid|calibrat|homeposition/i;
const ACTUAL=/actual|current|measured|feedback|monitoring|present|realtime/i;
const JOINT_LEAF = /^j([1-6])$/i;
const ORDINAL_JOINT = /^(?:joint|axis)([1-6])(?:position|angle)?$/i;
const AXIS_LEAF={x:'X',y:'Y',z:'Z',rx:'Rx',ry:'Ry',rz:'Rz',component1:'Rx',component2:'Ry',component3:'Rz'};
function resolveAxis(path){
 const atoms=path.split(PARTS).filter(Boolean);const leaf=norm(atoms.at(-1));
 if(AXIS_LEAF[leaf]){
  if(/^component[123]$/.test(leaf) && !/orientation|rotation|attitude|euler/i.test(atoms.slice(0,-1).join('.'))) return null;
  return {axis:AXIS_LEAF[leaf],group:atoms.slice(0,-1).join('.')};
 }
 const m=leaf.match(JOINT_LEAF)||leaf.match(ORDINAL_JOINT);
 if(m)return {axis:'J'+m[1],group:atoms.slice(0,-1).join('.')};
 if(leaf==='position'||leaf==='angle'){
  const p=norm(atoms.at(-2));const jm=p.match(JOINT_LEAF)||p.match(ORDINAL_JOINT);
  if(jm)return {axis:'J'+jm[1],group:atoms.slice(0,-2).join('.')};
 }
 const jm=leaf.match(/^j([1-6])(?:position|angle)$/i);
 if(jm)return {axis:'J'+jm[1],group:atoms.slice(0,-1).join('.')};
 return null;
}
const unsuitable = path => {
 const atoms=path.split(PARTS).filter(Boolean);
 return atoms.some((a,i)=>{
  const n=norm(a);
  if (i===atoms.length-1 && ['position','angle','x','y','z','rx','ry','rz','component1','component2','component3'].includes(n))return false;
  return BAD_CONTEXT.test(n) && !ACTUAL.test(n);
 });
};
function qualityOK(i){return !(i.usable===false && i.isGood===false) && !(/bad|uncertain/i.test(String(i.status||''))&&i.isGood!==true&&i.usable!==true);}
function addEntry(out, source, submodel, path, v, metadata={}){
 const val=numeric(v);if(val===null)return;
 const aasPath=path||'';
 const key=[submodel,aasPath].filter(Boolean).join('/');if(!key)return;
 const prior=out.get(key);
 if(!prior || prior.source!=='live'){
  out.set(key,{path:key,submodelIdShort:submodel||'',aasPath,value:val,source,...metadata});
 }
}
export function extractEntries(payload){
 const map=new Map();const invalid=new Set();const f=payload?.frame||{};
 for(const item of payload?.values||[]){
  const key=[item.submodelIdShort||'',item.path||item.id||''].filter(Boolean).join('/');
  if(!qualityOK(item)){invalid.add(key);map.delete(key);continue;}
  if(invalid.has(key))continue;
  addEntry(map,'live',item.submodelIdShort||'',item.path||item.id||'',item.value,{nodeId:item.nodeId||null});
 }
 for(const item of f.series||[]){
  const key=[item.submodelIdShort||'',item.path||item.id||''].filter(Boolean).join('/');
  if(invalid.has(key))continue;
  addEntry(map,'series',item.submodelIdShort||'',item.path||item.id||'',item.value,{nodeId:item.nodeId||null});
 }
 function walk(obj,parts=[],depth=0){
  if(depth>18 || !obj || typeof obj!=='object')return;
  for(const [k,v] of Object.entries(obj)){
   if(v && typeof v==='object')walk(v,[...parts,k],depth+1);
   else if(parts.length){
     const path=[...parts.slice(1),k].join('.');
     if(!invalid.has([parts[0],path].join('/')))addEntry(map,'snapshot',parts[0],path,v);
   }
  }
 }
 walk(f.snapshot||{});
 return [...map.values()].sort((a,b)=>a.path.localeCompare(b.path));
}
export function rankedCandidates(entries,axis){
 return entries.flatMap(e=>{
  const hit=resolveAxis(e.path);if(!hit||hit.axis!==axis||unsuitable(e.path))return [];
  const path=e.path.toLowerCase();
  if (!/robotkinematicstate|robotkinematics/i.test(path)) return [];
  const score=(/robotkinematicstate/i.test(path)?100:0)+(ACTUAL.test(path)?55:0)
    +(/jointpositions|jointangles|jointstate/i.test(path)&&axis.startsWith('J')?35:0)
    +(/tcp|cartesian|actualposition|pose/i.test(path)&&!axis.startsWith('J')?35:0)
    + (e.source==='live'?20:e.source==='series'?10:0)
    -(/operationalstate|status/i.test(path)?70:0);
  return [{...e,axis,group:hit.group,score}];
 }).sort((a,b)=>b.score-a.score||a.path.localeCompare(b.path));
}
export function autoMap(entries){
 const candidate=Object.fromEntries(LABELS.map(axis=>[axis,rankedCandidates(entries,axis)]));
 const chosen=Object.fromEntries(LABELS.map(axis=>[axis,null]));
 function fill(names){
  const buckets=new Map();
  for(const axis of names)for(const c of candidate[axis]){
   if(!buckets.has(c.group))buckets.set(c.group,{group:c.group,axes:new Map(),total:0});
   const b=buckets.get(c.group);if(!b.axes.has(axis))b.axes.set(axis,c);
  }
  const eligible=[...buckets.values()].filter(b=>names.every(n=>b.axes.has(n)));
  eligible.forEach(b=>b.total=names.reduce((s,n)=>s+b.axes.get(n).score,0));
  eligible.sort((a,b)=>b.total-a.total||a.group.localeCompare(b.group));
  if(eligible[0])names.forEach(n=>chosen[n]=eligible[0].axes.get(n));
 }
 fill(['X','Y','Z']);fill(['Rx','Ry','Rz']);fill(['J1','J2','J3','J4','J5','J6']);
 return {chosen,candidate,warnings:[
  ...(['X','Y','Z'].some(n=>!chosen[n])?['동일 출처의 실제 TCP XYZ 세트가 확인되지 않았습니다.']:[]),
  ...(['J1','J2','J3','J4','J5','J6'].some(n=>!chosen[n])?['동일 출처의 실제 J1~J6 세트가 확인되지 않았습니다.']:[])
 ]};
}
export function bindingKey(binding){
 if(!binding)return '';
 if(binding.key)return binding.key;
 return [binding.submodelIdShort,binding.path||binding.aasPath].filter(Boolean).join('/');
}
function entryMatchesBinding(entry,binding){
 if(!entry||!binding)return false;
 if(binding.key && entry.path===binding.key)return true;
 const sub=binding.submodelIdShort||'';
 const aas=binding.path||binding.aasPath||'';
 return (entry.submodelIdShort||'')===sub && (entry.aasPath||'')===aas;
}
export function resolveChannels(entries,manual,aasBindings={}){
 const auto=autoMap(entries);
 const index=new Map(entries.map(e=>[e.path,e]));
 const chosen={};
 for(const n of LABELS){
  if(manual[n] && manual[n]!=='auto'){
   chosen[n]=index.get(manual[n])||null;
   continue;
  }
  const binding=aasBindings[n];
  chosen[n]=binding? (entries.find(e=>entryMatchesBinding(e,binding))||null) : auto.chosen[n];
 }
 return {chosen,auto};
}
