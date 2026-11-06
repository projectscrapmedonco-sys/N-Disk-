/* Tamil-to-English clinical draft and nurse CRF verification workflow.
   Uploaded audio is sent only to a same-origin transcription endpoint after nurse confirmation. */
(function(){
  const crfBaseNewV=newV;
  const crfBaseVisit=visit;
  const crfBaseSummary=summary;
  const crfBaseSetSev=setSev;
  const crfBaseRender=render;
  const AUDIO_UPLOAD={file:null,url:"",key:"",busy:false,sendApproved:false};
  const CARE_SCRIBE_ORIGINS=["http://localhost:10125","http://127.0.0.1:10125"];
  let careScribeFrame=null;

  const hasTamil=t=>/[\u0B80-\u0BFF]/.test(String(t||""));
  const stamp=()=>new Date().toLocaleString();
  const clone=o=>JSON.parse(JSON.stringify(o));
  const severity=i=>SEV[+i||0]||"Nil";

  function structuredDataLines(value,prefix,out,depth,seen){
    out=out||[];depth=depth||0;seen=seen||new Set();
    if(value===null||value===undefined||depth>5)return out;
    if(typeof value==="string"||typeof value==="number"||typeof value==="boolean"){
      const text=value===true?"yes":value===false?"no":String(value);
      if(text.trim())out.push((prefix?prefix+": ":"")+text);
      return out;
    }
    if(typeof value!=="object"||seen.has(value))return out;
    seen.add(value);
    if(Array.isArray(value)){
      value.forEach((item,index)=>structuredDataLines(item,prefix||("item "+(index+1)),out,depth+1,seen));
      return out;
    }
    Object.entries(value).forEach(([key,item])=>structuredDataLines(item,prefix?prefix+" "+key:key,out,depth+1,seen));
    return out;
  }
  function structuredDataText(data){return structuredDataLines(data,"",[],0,new Set()).join("\n");}
  function structuredDataFieldCount(data){return structuredDataLines(data,"",[],0,new Set()).length;}

  function blankCRF(){
    return{status:"draft",reviewer:"",confirmation:false,version:0,submittedAt:"",
      reopenedReason:"",suggested:null,changes:[],editLogged:false};
  }
  function ensureVisit(v){
    if(!v)return v;
    if(typeof v.enTx!=="string")v.enTx="";
    if(!v.translation||typeof v.translation!=="object"){
      v.translation={source:"",language:"",draftedAt:"",checked:false,unresolved:0};
    }
    if(!v.crf||typeof v.crf!=="object")v.crf=blankCRF();
    if(!v.audio||typeof v.audio!=="object"){
      v.audio={name:"",type:"",size:0,language:"ta-IN",status:"",message:"",transcribedAt:""};
    }
    if(!v.audio.structuredData||typeof v.audio.structuredData!=="object")v.audio.structuredData={};
    return v;
  }

  newV=function(p){
    clearUploadedAudio();
    return ensureVisit(crfBaseNewV(p));
  };

  function currentVisitKey(){
    return S.p?S.p.id+"|"+((S.v||{}).h||{}).tag:"";
  }
  function clearUploadedAudio(){
    try{if(AUDIO_UPLOAD.url&&window.URL)window.URL.revokeObjectURL(AUDIO_UPLOAD.url)}catch(e){}
    AUDIO_UPLOAD.file=null;
    AUDIO_UPLOAD.url="";
    AUDIO_UPLOAD.key="";
    AUDIO_UPLOAD.busy=false;
    AUDIO_UPLOAD.sendApproved=false;
  }
  function allowedAudio(file){
    const type=String(file.type||"").toLowerCase(),name=String(file.name||"");
    return type.startsWith("audio/")||/\.(aac|m4a|mp3|mp4|ogg|wav|webm)$/i.test(name);
  }
  function audioState(v){
    if(AUDIO_UPLOAD.key!==currentVisitKey())return null;
    return AUDIO_UPLOAD.file?AUDIO_UPLOAD:null;
  }
  function chooseVisitAudio(file){
    const v=ensureVisit(S.v);
    if(!v||!S.p||S.role!=="nurse")return;
    if(v.crf.status==="submitted"){alert("This CRF is submitted and locked. Reopen it before changing the audio source.");return}
    if(!file)return;
    if(!allowedAudio(file)){alert("Choose an audio file such as WAV, MP3, M4A, OGG, or WebM.");return}
    if(file.size>52428800){alert("Audio must be 50 MB or smaller.");return}
    clearUploadedAudio();
    AUDIO_UPLOAD.file=file;
    AUDIO_UPLOAD.key=currentVisitKey();
    try{AUDIO_UPLOAD.url=window.URL.createObjectURL(file)}catch(e){}
    v.audio={name:file.name||"audio recording",type:file.type||"audio",size:file.size||0,
      language:(v.audio||{}).language||"ta-IN",status:"Ready to transcribe",message:"",transcribedAt:""};
    log("Audio selected for transcription ("+S.p.id+"; "+Math.ceil(file.size/1024)+" KB)");
    render();
  }
  function setAudioLanguage(value){
    const v=ensureVisit(S.v);
    if(v&&v.crf.status!=="submitted")v.audio.language=value||"ta-IN";
  }
  function setAudioEndpoint(value){
    S.audioEndpoint=String(value||"").trim()||"/api/transcribe";
    save();
  }
  function setAudioSendApproved(checked){
    AUDIO_UPLOAD.sendApproved=!!checked;
  }
  function discardUploadedAudio(){
    const v=ensureVisit(S.v);
    if(!v||v.crf.status==="submitted")return;
    const language=(v.audio||{}).language||"ta-IN";
    clearUploadedAudio();
    v.audio={name:"",type:"",size:0,language,status:"",message:"",transcribedAt:""};
    render();
  }
  function audioEndpoint(){
    const raw=String(S.audioEndpoint||"/api/transcribe").trim()||"/api/transcribe";
    if(!raw.startsWith("/")||raw.startsWith("//")){
      throw new Error("Use a same-origin path such as /api/transcribe.");
    }
    const endpoint=new URL(raw,location.href);
    if(endpoint.origin!==location.origin)throw new Error("The transcription endpoint must stay on this app's origin.");
    return endpoint.href;
  }
  function audioErrorMessage(error){
    const message=String((error||{}).message||error||"Could not transcribe this audio.");
    if(/failed to fetch|networkerror|network request failed/i.test(message)){
      return "No transcription service is running at "+(S.audioEndpoint||"/api/transcribe")+". Start or deploy the same-origin transcription backend, then retry. You can still paste a transcript and use the Tamil → English CRF workflow.";
    }
    return message;
  }

  function closeCareScribe(){
    const overlay=document.querySelector("#care-scribe-overlay");
    if(overlay)overlay.remove();
    careScribeFrame=null;
  }

  function openCareScribe(){
    const v=ensureVisit(S.v);
    if(!v||!S.p||S.role!=="nurse")return;
    if(v.crf.status==="submitted"){
      alert("This CRF is submitted and locked. Reopen it before starting a new Scribe session.");
      return;
    }
    if(!v.consent){alert("Record consent before opening CARE AI Scribe.");return}
    closeCareScribe();
    const overlay=document.createElement("div");
    overlay.id="care-scribe-overlay";
    overlay.style.cssText="position:fixed;inset:0;z-index:10000;background:rgba(15,23,42,.72);display:flex;align-items:center;justify-content:center;padding:16px";
    overlay.innerHTML='<div style="position:relative;width:min(760px,100%);height:min(760px,94vh);overflow:hidden;border-radius:18px;background:#f8fafc;box-shadow:0 24px 80px rgba(0,0,0,.35)"><button id="care-scribe-close" style="position:absolute;right:10px;top:10px;z-index:2;width:34px;height:34px;border:0;border-radius:999px;background:#ffffff;color:#334155;font-size:22px;line-height:1;cursor:pointer" aria-label="Close CARE AI Scribe">×</button><iframe title="CARE AI Scribe" src="http://localhost:10125/standalone.html" style="width:100%;height:100%;border:0;background:#f8fafc"></iframe></div>';
    document.body.appendChild(overlay);
    overlay.querySelector("#care-scribe-close").addEventListener("click",closeCareScribe);
    careScribeFrame=overlay.querySelector("iframe");
  }
  window.openCareScribe=openCareScribe;
  window.closeCareScribe=closeCareScribe;

  window.addEventListener("message",event=>{
    if(!CARE_SCRIBE_ORIGINS.includes(event.origin)||!careScribeFrame||event.source!==careScribeFrame.contentWindow)return;
    const data=event.data||{};
    if(data.source!=="care-ai-scribe")return;
    const v=ensureVisit(S.v);
    if(!v||!S.p||S.role!=="nurse")return;
    if(data.type==="transcript"||data.type==="result"){
      const transcript=String(data.transcript||"").trim();
       const structuredData=data.structuredData&&typeof data.structuredData==="object"?data.structuredData:{};
       if(transcript||structuredDataFieldCount(structuredData)){
         if(transcript)v.tx=transcript;
          v.audio.structuredData=structuredData;
        v.audio.status="CARE AI Scribe transcript received";
         v.audio.message=structuredDataFieldCount(structuredData)?"Eka Scribe returned structured values; recognised CRF fields were drafted below for nurse review.":"Review the transcript in this workflow, then translate/draft and verify the CRF before submission.";
        v.audio.transcribedAt=stamp();
        log("CARE AI Scribe transcript received for "+S.p.id+"; nurse review required.");
        if(data.type==="result"){
           extractDraft({english:String(data.englishTranscript||data.english||"").trim(),structuredData});
        }else{
          render();
        }
      }
    }else if(data.type==="error"){
      v.audio.status="CARE AI Scribe unavailable";
      v.audio.message=String(data.message||"The CARE AI Scribe session failed.");
      render();
    }
  });
  async function transcribeUploadedAudio(){
    const v=ensureVisit(S.v),upload=audioState(v);
    if(!v||!S.p||S.role!=="nurse")return;
    if(v.crf.status==="submitted"){alert("This CRF is submitted and locked. Reopen it before changing the audio source.");return}
    if(!v.consent){alert("Record consent before sending audio for transcription.");return}
    if(!upload){alert("Choose an audio file first.");return}
    if(!AUDIO_UPLOAD.sendApproved){
      alert("Confirm that you have authority to send this consented recording to the approved transcription service.");
      return;
    }
    const endpointInput=document.querySelector("#audio-endpoint"),languageInput=document.querySelector("#audio-language");
    if(endpointInput)setAudioEndpoint(endpointInput.value);
    if(languageInput)setAudioLanguage(languageInput.value);
    let endpoint;
    try{endpoint=audioEndpoint()}catch(e){alert(e.message);return}
    upload.busy=true;
    v.audio.status="Sending audio for transcription…";
    v.audio.message="";
    render();
    try{
      const data=new FormData();
      data.append("audio",upload.file,v.audio.name||upload.file.name||"recording");
      data.append("language",v.audio.language||"ta-IN");
      const response=await fetch(endpoint,{method:"POST",body:data,credentials:"same-origin",headers:{Accept:"application/json"}});
      let body={};
      try{body=await response.json()}catch(e){}
      if(!response.ok)throw new Error(body.detail||body.message||("The transcription service returned "+response.status+"."));
      const transcript=String(body.transcript||body.text||"").trim();
      const structuredData=body.structured_data&&typeof body.structured_data==="object"?body.structured_data:(body.structuredData&&typeof body.structuredData==="object"?body.structuredData:{});
      const structuredText=structuredDataText(structuredData);
      if(!transcript&&!structuredDataFieldCount(structuredData))throw new Error("The transcription service returned no transcript or structured clinical note.");
      v.tx=transcript||structuredText;
      v.audio.status="Transcript received";
      const qualityWarning=String(body.quality_warning||body.qualityWarning||"").trim();
      v.audio.structuredData=structuredData;
      v.audio.message=qualityWarning||(structuredDataFieldCount(structuredData)?"Structured CRF draft is ready for nurse review.":"Tamil/English draft and CRF suggestions are ready for nurse review.");
      v.audio.transcribedAt=stamp();
      v.audio.language=String(body.language||v.audio.language||"ta-IN");
      upload.busy=false;
      log("Audio transcription received for "+S.p.id+"; nurse review required.");
      if(qualityWarning){
        // Do not turn a low-confidence transcript into clinical facts. Keep it
        // editable so the nurse can correct it and explicitly draft the CRF.
        v.enTx="";
        v.translation={source:v.tx,language:"Needs nurse review",draftedAt:"",checked:false,unresolved:1};
        render();
      }else{
        extractDraft({english:String(body.english_transcript||body.englishTranscript||body.translation||"").trim(),structuredData});
      }
    }catch(e){
      upload.busy=false;
      v.audio.status="Transcription unavailable";
      v.audio.message=audioErrorMessage(e);
      render();
    }
  }
  window.chooseVisitAudio=chooseVisitAudio;
  window.setAudioLanguage=setAudioLanguage;
  window.setAudioEndpoint=setAudioEndpoint;
  window.setAudioSendApproved=setAudioSendApproved;
  window.discardUploadedAudio=discardUploadedAudio;
  window.transcribeUploadedAudio=transcribeUploadedAudio;

  function ecogFrom(text){
    const m=String(text||"").match(/(?:ecog|ஈகாக்|ஈகோக்|இகாக்)\D{0,12}([0-5])/i);
    return m?m[1]:"";
  }
  function symptomFacts(text,c){
    const source=clauses(String(text||"")),facts=[],seen={};
    const collect=(kind,list)=>{
      list.forEach((entry,index)=>{
        const line=source.find(part=>kwRe(entry).test(part));
        if(!line)return;
        const key=kind+index;
        if(seen[key])return;
        seen[key]=1;
        facts.push({kind,index,label:entry.split("|")[0],value:levelOf(line,entry.split("|").slice(1).includes("appetite"))});
      });
    };
    collect("rec",c.rec);
    collect("se",c.se);
    return facts;
  }
  function findingSentence(f){
    const label=f.label.replace(/\s*\([^)]*\)/g,"").toLowerCase();
    const clinicalLabel=f.label.includes("(")?f.label.toLowerCase():label;
    if(f.value===0)return"The patient denies "+clinicalLabel+".";
    return"The patient reports "+severity(f.value).toLowerCase()+" "+clinicalLabel+".";
  }
  function englishClinicalDraft(source,supplemental){
    const combined=[source,supplemental].filter(Boolean).join("\n"),tamil=hasTamil(source),c=CA[S.p.c],facts=symptomFacts(combined,c),lines=facts.map(findingSentence);
    const ecog=ecogFrom(combined);
    if(ecog)lines.push("ECOG performance status: "+ecog+".");
    const tamilLines=clauses(source).filter(hasTamil);
    const unresolved=Math.max(0,tamilLines.length-facts.length);
    if(!tamil){
      return{text:String(combined||"").trim(),tamil:false,unresolved:0,facts,ecog};
    }
    if(!lines.length){
      lines.push("Nurse translation required: no recognised clinical statement was found in the Tamil transcript.");
    }
    if(unresolved){
      lines.push("Additional Tamil statement(s) require nurse translation before the CRF is submitted.");
    }
    return{text:lines.join("\n"),tamil:true,unresolved,facts,ecog};
  }
  function setDraftState(v){
    ensureVisit(v);
    if(v.crf.status==="submitted")return;
    v.crf.status="draft";
    v.crf.confirmation=false;
  }
  function suggestionSnapshot(v){
    const fields={};
    Object.keys(v.ai||{}).forEach(key=>{
      let m=key.match(/^(rec|se)(\d+)$/);
      if(m){
        const kind=m[1],index=+m[2],entry=CA[S.p.c][kind][index];
        if(entry)fields[key]={label:entry.split("|")[0],value:+v[kind][index]||0};
      }else if(key==="ecog"){
        fields.ecog={label:"ECOG performance status",value:String(v.ecog||"")};
      }else{
        m=key.match(/^cc(\d+)$/);
        if(m)fields[key]={label:"Chief complaint "+(+m[1]+1),value:String(v.cc[+m[1]]||"")};
      }
    });
    return{fields,english:String(v.enTx||"")};
  }
  function suggestedChanges(v){
    const suggested=v.crf.suggested;
    if(!suggested)return[];
    const changes=[];
    Object.keys(suggested.fields||{}).forEach(key=>{
      const before=suggested.fields[key],m=key.match(/^(rec|se)(\d+)$/);
      let after;
      if(m)after=+v[m[1]][+m[2]]||0;
      else if(key==="ecog")after=String(v.ecog||"");
      else{
        const cc=key.match(/^cc(\d+)$/);
        after=cc?String(v.cc[+cc[1]]||""):"";
      }
      if(String(before.value)!==String(after)){
        const oldValue=m?severity(before.value):before.value||"Not recorded";
        const newValue=m?severity(after):after||"Not recorded";
        changes.push(before.label+": "+oldValue+" → "+newValue);
      }
    });
    if(String(suggested.english||"")!==String(v.enTx||"")){
      changes.push("English clinical transcript edited by nurse");
    }
    return changes;
  }
  function markEdited(){
    const v=ensureVisit(S.v);
    if(!v||v.crf.status==="submitted")return;
    setDraftState(v);
    if(!v.crf.editLogged){
      v.crf.editLogged=true;
      log("Nurse began reviewing or editing CRF values ("+S.p.id+")");
    }
  }
  function extractDraft(options){
    const v=ensureVisit(S.v);
    if(!v||!S.p)return;
    // Accept either the original source box or the editable English box. This
    // lets a nurse paste a transcript into the visible English draft area and
    // still draft the CRF when the upstream transcription service is down.
    const rawSource=String(v.tx||v.enTx||"").trim();
    const structuredData=options&&options.structuredData&&typeof options.structuredData==="object"?options.structuredData:{};
    const structuredText=structuredDataText(structuredData);
    const structuredNotes=typeof structuredData.clinical_notes==="string"?structuredData.clinical_notes.trim():"";
    const source=rawSource||structuredNotes||structuredText;
    if(!source){alert("Record, type, or paste the Tamil/English transcript first.");return}
    if(!String(v.tx||"").trim())v.tx=source;
    const draft=englishClinicalDraft(source,structuredText);
    const serviceEnglish=String(((options||{}).english)||structuredNotes||"").trim();
    v.enTx=serviceEnglish||draft.text;
    v.audio.structuredData=structuredData;
    v.translation={source,language:structuredDataFieldCount(structuredData)?"Eka Scribe structured CRF draft":(serviceEnglish?"English clinical draft from transcription service":(draft.tamil?"Tamil → English clinical draft":"English transcript")),
      draftedAt:stamp(),checked:false,unresolved:serviceEnglish?0:draft.unresolved};
    v.ai={};
    const c=CA[S.p.c],segments=clauses(source+"\n"+structuredText+"\n"+v.enTx);
    let filled=0;
    const run=(kind,list)=>list.forEach((entry,index)=>{
      const line=segments.find(part=>kwRe(entry).test(part));
      if(!line)return;
      v[kind][index]=levelOf(line,entry.split("|").slice(1).includes("appetite"));
      v.ai[kind+index]=1;
      filled++;
    });
    run("rec",c.rec);
    run("se",c.se);
    const adviceFilled=window.applyTranscriptAdvice?window.applyTranscriptAdvice(v,c,source+"\n"+v.enTx):0;
    const ecog=ecogFrom(source+"\n"+v.enTx);
    if(ecog){
      v.ecog=ecog;
      v.ai.ecog=1;
      filled++;
    }
    const complaintFacts=symptomFacts(source+"\n"+v.enTx,c).filter(f=>f.value>0).slice(0,3);
    complaintFacts.forEach((fact,index)=>{
      if(v.cc[index])return;
      v.cc[index]=severity(fact.value)+" "+fact.label.toLowerCase();
      v.ai["cc"+index]=1;
      filled++;
    });
    setDraftState(v);
    v.crf.suggested=suggestionSnapshot(v);
    v.crf.changes=[];
    v.crf.editLogged=false;
    const draftLabel=structuredDataFieldCount(structuredData)?"Eka Scribe structured draft prepared; ":(serviceEnglish?"Transcription-service English draft prepared; ":(draft.tamil?"Tamil clinical draft created; ":"English transcript prepared; "));
    log(draftLabel+"auto-filled "+filled+" CRF field(s) and copied "+adviceFilled+" explicit advice item(s) ("+S.p.id+"). Nurse review required.");
    render();
  }

  extract=extractDraft;
  window.translateAndDraftCRF=extractDraft;

  setSev=function(kind,index,value){
    const v=ensureVisit(S.v);
    if(v&&v.crf.status==="submitted"){
      alert("This CRF is submitted and locked. Use Reopen CRF to make a corrected version.");
      return;
    }
    markEdited();
    crfBaseSetSev(kind,index,value);
  };

  function setEnglishDraft(value){
    const v=ensureVisit(S.v);
    if(!v||v.crf.status==="submitted")return;
    v.enTx=value;
    v.translation.checked=false;
    markEdited();
  }
  function setTranslationReviewed(checked){
    const v=ensureVisit(S.v);
    if(!v||v.crf.status==="submitted")return;
    v.translation.checked=!!checked;
  }
  function setCrfReviewer(value){
    const v=ensureVisit(S.v);
    if(!v||v.crf.status==="submitted")return;
    v.crf.reviewer=value;
  }
  function setCrfConfirmation(checked){
    const v=ensureVisit(S.v);
    if(!v||v.crf.status==="submitted")return;
    v.crf.confirmation=!!checked;
  }
  window.setEnglishDraft=setEnglishDraft;
  window.setTranslationReviewed=setTranslationReviewed;
  window.setCrfReviewer=setCrfReviewer;
  window.setCrfConfirmation=setCrfConfirmation;

  function snapshotForSubmission(v){
    const copy=clone(v);
    delete copy.crf.suggested;
    if(copy.audio){
      delete copy.audio.file;
      delete copy.audio.url;
    }
    return copy;
  }
  function verifyAndSubmitCRF(){
    const v=ensureVisit(S.v);
    if(!v||!S.p)return;
    const reviewer=((document.querySelector("#crf-reviewer")||{}).value||v.crf.reviewer||"").trim();
    const confirmed=!!((document.querySelector("#crf-confirm")||{}).checked||v.crf.confirmation);
    if(!reviewer){alert("Enter the nurse name or identifier before submitting.");return}
    if(hasTamil(v.tx)&&!v.translation.checked){
      alert("Confirm that you reviewed the English translation before submitting.");
      return;
    }
    if(!confirmed){
      alert("Confirm that you reviewed the transcript and CRF values before submitting.");
      return;
    }
    const detected=Object.keys(v.rec||{}).length+Object.keys(v.se||{}).length+Number(v.ecog!=="");
    if(!detected&&!confirm("No symptom or ECOG values are recorded. Submit this CRF anyway?"))return;
    const changes=suggestedChanges(v);
    if(!confirm("Submit this CRF for "+S.p.id+"? A correction will create a new version."))return;
    S.crfs=S.crfs||{};
    const key=S.p.id+"|"+v.h.tag;
    const versions=S.crfs[key]||[];
    const submittedAt=stamp();
    const version=versions.length+1;
    v.crf.status="submitted";
    v.crf.reviewer=reviewer;
    v.crf.confirmation=true;
    v.crf.version=version;
    v.crf.submittedAt=submittedAt;
    v.crf.changes=changes;
    versions.push({version,submittedAt,reviewer,translationReviewed:!!v.translation.checked,
      changes:clone(changes),snapshot:snapshotForSubmission(v)});
    S.crfs[key]=versions;
    log("CRF v"+version+" verified and submitted by "+reviewer+" for "+S.p.id+
      (changes.length?"; "+changes.length+" AI suggestion change(s) recorded.":"."));
    save();
    render();
  }
  window.verifyAndSubmitCRF=verifyAndSubmitCRF;

  function reopenCRF(){
    const v=ensureVisit(S.v);
    if(!v||v.crf.status!=="submitted")return;
    const reason=prompt("Reason for reopening this submitted CRF (required):");
    if(!reason||!reason.trim())return;
    v.crf.status="draft";
    v.crf.confirmation=false;
    v.translation.checked=false;
    v.crf.reopenedReason=reason.trim();
    v.crf.editLogged=false;
    log("Submitted CRF reopened for correction by nurse: "+v.crf.reopenedReason+" ("+S.p.id+")");
    render();
  }
  window.reopenCRF=reopenCRF;

  summary=function(){
    const v=ensureVisit(S.v);
    if(!v||v.crf.status!=="submitted"){
      alert("Verify changes and submit the CRF before generating the visit summary.");
      return;
    }
    if(S.sum){window.print();return}
    crfBaseSummary();
  };

  function audioUploadCard(v){
    const audio=v.audio||{},upload=audioState(v),ready=!!upload&&!upload.busy;
    const language=audio.language||"ta-IN";
    const sourceInfo=upload?'<div class="sub" style="margin-top:6px">Selected: '+esc(audio.name||"audio recording")+' · '+Math.ceil((audio.size||0)/1024)+' KB</div>':'<div class="sub" style="margin-top:6px">No audio selected. The file stays in this tab until you explicitly start transcription.</div>';
    const player=upload&&upload.url?'<audio controls src="'+esc(upload.url)+'" style="width:100%;margin-top:8px"></audio>':"";
    const status=audio.status?'<div class="note" style="margin-top:8px">'+esc(audio.status)+(audio.message?'<br><span class="sub">'+esc(audio.message)+'</span>':"")+'</div>':"";
    const action=upload&&upload.busy?"Transcribing audio…":"Transcribe audio & draft CRF";
    const recording=REC.on?'<button class="b" onclick="recStop()">■ Stop & transcribe</button><span class="tag r">● recording</span>':(REC.busy?'<button class="b" disabled>Transcribing…</button><span class="tag w">● processing</span>':'<button class="b" onclick="recStart()">● Start recording</button>');
    const recordingPlayer=REC.url?'<audio controls src="'+esc(REC.url)+'" style="width:100%;margin-top:8px"></audio><button class="b g" style="margin-top:6px" onclick="recDiscard()">Discard recording</button>':"";
    return '<div class="card" data-audio-upload><h2>Audio recorder & transcription</h2>'+ 
      '<div class="note"><b>Built-in recorder:</b> Record Tamil or English, then review the transcript before drafting the CRF. Audio is sent only after consent.</div>'+
      '<div class="recorder-panel"><div class="row"><div><b>Built-in microphone recorder</b><div class="sub">Use Chrome or Edge and allow microphone access.</div></div><div class="row" style="justify-content:flex-end">'+recording+'</div></div>'+recordingPlayer+(REC.msg?'<div class="sub" style="margin-top:8px">'+esc(REC.msg)+'</div>':"")+'</div>'+ 
      '<div class="row" style="margin-top:12px"><button class="b g" onclick="openCareScribe()">Optional: Open CARE AI Scribe</button><span class="sub">Use this only when Eka Scribe is configured.</span></div>'+ 
      '<h3 style="margin-top:16px">Upload an existing recording</h3>'+ 
      '<label>Audio recording (WAV, MP3, M4A, OGG, or WebM; max 50 MB)</label><input id="visit-audio" type="file" accept="audio/*,.aac,.m4a,.mp3,.mp4,.ogg,.wav,.webm" onchange="chooseVisitAudio(this.files[0])">'+
      sourceInfo+player+
      '<div class="grid" style="margin-top:10px"><div><label>Spoken language</label><select id="audio-language" onchange="setAudioLanguage(this.value)">'+
        [["ta-IN","Tamil"],["en-IN","English (India)"],["auto","Auto-detect"]].map(option=>'<option value="'+option[0]+'" '+(language===option[0]?"selected":"")+'>'+option[1]+'</option>').join("")+
      '</select></div><div><label>Same-origin transcription path</label><input id="audio-endpoint" value="'+esc(S.audioEndpoint||"/api/transcribe")+'" placeholder="/api/transcribe" onchange="setAudioEndpoint(this.value)"></div></div>'+
      '<label style="margin-top:10px"><input id="audio-send-confirm" type="checkbox" style="width:auto" '+(AUDIO_UPLOAD.sendApproved?"checked":"")+' onchange="setAudioSendApproved(this.checked)"> I confirmed consent and have authority to send this recording to the approved transcription service.</label>'+
      '<div class="row" style="margin-top:8px"><button class="b" onclick="transcribeUploadedAudio()" '+(ready?"":"disabled")+">"+action+'</button>'+
      (upload?'<button class="b g" onclick="discardUploadedAudio()">Discard audio</button>':"")+'</div>'+status+'</div>';
  }

  function careScribeCard(v){
    const audio=v.audio||{};
    const status=audio.status?'<div class="note" style="margin-top:8px">'+esc(audio.status)+(audio.message?'<br><span class="sub">'+esc(audio.message)+'</span>':"")+'</div>':"";
    return '<div class="card"><h2>1 · CARE AI Scribe</h2>'+
      '<div class="note">Record live audio or upload a Tamil/English recording in CARE AI Scribe. The transcript returns here and drafts the important CRF fields. Your nurse reviews the English transcript, edits the CRF, verifies changes, and submits it in this PWA.</div>'+
      '<button class="b" style="margin-top:8px" onclick="openCareScribe()">🎙 Open CARE AI Scribe</button>'+status+
      '<div class="sub" style="margin-top:8px">Consent is required before opening Scribe. CARE AI Scribe is the transcription service; this page remains your workflow.</div></div>';
  }

  function replaceLegacyAudioCard(base,v){
    const startMarker='<div class="grid"><div class="card"><h2>1 · Record, upload & transcribe</h2>';
    const endMarker='<div class="card"><h2>2 · Chief complaints</h2>';
    const start=base.indexOf(startMarker),end=base.indexOf(endMarker);
    if(start<0||end<=start)return base;
    return base.slice(0,start)+'<div class="grid visit-intake-grid">'+audioUploadCard(v)+base.slice(end);
  }
  function translationCard(v){
    const t=v.translation,status=t.source?(t.checked?"English translation reviewed by nurse":"English translation needs nurse review"):"No English draft yet";
    const unresolved=t.unresolved?'<div class="note" style="margin-top:8px">Some Tamil statements were not recognised. Add or correct their English meaning before you submit.</div>':"";
    return '<div class="card" data-crf-translation><h2>Tamil → English clinical transcript</h2>'+
      '<div class="note">The Tamil source transcript remains in the Record & transcribe box. This offline clinical draft only converts the proforma’s recognised symptom language; it is not a certified medical translation. Check and correct it before submission.</div>'+
      '<label>Editable English clinical transcript</label><textarea id="english-transcript" style="min-height:140px" oninput="setEnglishDraft(this.value)">'+esc(v.enTx||"")+'</textarea>'+
      '<div class="row" style="margin-top:8px"><button class="b" onclick="translateAndDraftCRF()">Translate Tamil → English & draft CRF</button><span class="tag '+(t.checked?"":"w")+'">'+esc(status)+'</span></div>'+
      '<label style="margin-top:10px"><input type="checkbox" style="width:auto" '+(t.checked?"checked":"")+' onchange="setTranslationReviewed(this.checked)"> I reviewed and corrected the English transcript where needed</label>'+
      unresolved+'</div>';
  }
  function downloadFilledCRF(){
    const v=ensureVisit(S.v),p=S.p;
    if(!v||!p){alert("Open a patient visit before downloading the CRF.");return}
    const c=CA[p.c]||{rec:[],se:[]};
    const rows=(kind,list)=>(list||[]).map((entry,index)=>({label:entry.split("|")[0],severity:SEV[+((v[kind]||[])[index]||0)]||"Nil",advice:(v.adv||{})[kind+index]||""}));
    const now=new Date();
    const payload={cancer:p.c,patient:{id:p.id||"",name:p.name||"",age:p.age||"",sex:p.sex||""},visit:{tag:v.h&&v.h.tag||"T1",planned:!!(v.h&&v.h.pl),mode:v.h&&v.h.mode||"",provider:v.h&&v.h.pv||"",caregiver:v.caregiver||"",date:now.toLocaleDateString(),time:now.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}),complaints:Array.isArray(v.cc)?v.cc.filter(Boolean):[],recurrence:rows("rec",c.rec),side_effects:rows("se",c.se),other_symptoms:v.other||"",ecog:v.ecog||"",status:v.crf&&v.crf.status||"draft"}};
    fetch("/api/download-crf",{method:"POST",headers:{"Content-Type":"application/json",Accept:"application/pdf"},credentials:"same-origin",body:JSON.stringify(payload)}).then(async response=>{
       if(!response.ok){let detail=response.status===404?"Original-format PDF export is unavailable. Run setup-local-transcription.bat, then run-local.bat so the Python service is running.":"PDF export failed.";try{detail=(await response.json()).detail||detail}catch(e){}throw new Error(detail)}
      const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");
      a.href=url;a.download=(p.id||"patient")+"-visit-crf.pdf";document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);log("Downloaded original-format CRF for "+p.id);
    }).catch(error=>alert(error.message||"Could not download the original-format CRF."));
  }
  window.downloadFilledCRF=downloadFilledCRF;

  function reviewCard(v){
    const submitted=v.crf.status==="submitted",changes=v.crf.changes||[];
    if(submitted){
      const list=changes.length?'<ul style="margin:8px 0;padding-left:20px">'+changes.map(change=>'<li>'+esc(change)+'</li>').join("")+'</ul>':'<div class="sub" style="margin-top:8px">No AI-suggested fields were changed before submission.</div>';
      return '<div class="card" data-crf-submit-controls><div class="row"><div><h2>CRF submitted <span class="tag">v'+v.crf.version+'</span></h2><div class="sub">Verified by '+esc(v.crf.reviewer)+' · '+esc(v.crf.submittedAt)+'</div></div><span class="tag">Locked</span></div>'+list+
        '<div class="note" style="margin-top:8px">The submitted version is saved in the encrypted nurse workspace. Reopening creates a corrected future version and keeps this submission record.</div>'+
        '<div class="row" style="margin-top:8px"><button class="b g" onclick="downloadFilledCRF()">Download filled CRF</button><button class="b g" onclick="reopenCRF()">Reopen CRF for correction</button></div></div>';
    }
    const changeHint=v.crf.suggested?'<div class="sub" style="margin-top:6px">AI-suggested values will be compared with the final values and changes will be recorded at submission.</div>':'<div class="sub" style="margin-top:6px">Use “Translate Tamil → English & draft CRF” or complete the CRF manually before submitting.</div>';
    return '<div class="card" data-crf-submit-controls><h2>Verify changes & submit CRF</h2>'+
      '<div class="note">Submission records the nurse identifier, review time, translation review, and changes made before submission.</div>'+ 
      '<label>Nurse name or identifier</label><input id="crf-reviewer" value="'+esc(v.crf.reviewer||"")+'" placeholder="e.g. Nurse R. Kumar" oninput="setCrfReviewer(this.value)">'+
      '<label style="margin-top:10px"><input id="crf-confirm" type="checkbox" style="width:auto" '+(v.crf.confirmation?"checked":"")+' onchange="setCrfConfirmation(this.checked)"> I reviewed the English transcript, corrected the CRF as needed, and approve this submission.</label>'+
      changeHint+'<div class="row" style="margin-top:10px"><button class="b" onclick="verifyAndSubmitCRF()">✓ Verify changes & submit CRF</button><button class="b g" onclick="downloadFilledCRF()">Download filled CRF</button></div></div>';
  }
  visit=function(){
    const base=crfBaseVisit();
    if(!S.v||S.role==="patient")return base;
    const v=ensureVisit(S.v);
    return replaceLegacyAudioCard(base,v)+translationCard(v)+reviewCard(v);
  };

  function lockSubmittedUI(){
    if(S.locked||S.tab!=="Tele-visit"||!S.v)return;
    const v=ensureVisit(S.v),app=document.querySelector("#app");
    if(!app)return;
    app.querySelectorAll("button[onclick='summary()']").forEach(button=>button.dataset.crfSubmitControl="1");
    if(v.crf.status==="submitted"){
      app.querySelectorAll("input,textarea,select,button").forEach(control=>{
        if(!control.closest("[data-crf-submit-controls]")&&!control.matches("[data-crf-submit-control]"))control.disabled=true;
      });
    }
  }
  document.addEventListener("change",event=>{
    const target=event.target;
    if(!target||!target.closest||!target.closest("#app")||target.closest("[data-crf-submit-controls]")||target.closest("[data-audio-upload]"))return;
    if(S.tab==="Tele-visit"&&S.v)markEdited();
  },true);
  render=function(){
    crfBaseRender();
    setTimeout(lockSubmittedUI,0);
  };
  render();
})();
