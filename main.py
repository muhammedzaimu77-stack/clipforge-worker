from __future__ import annotations
import os, re, json, uuid, shutil, subprocess, logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl, Field
from sqlalchemy import create_engine, String, Integer, Float, Text, DateTime, ForeignKey, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from celery import Celery
from scoring import score_moment, segment_words, heuristic_signals

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log=logging.getLogger("clipforge")
DATA=Path(os.getenv("CLIPFORGE_DATA","/data")).resolve(); DATA.mkdir(parents=True,exist_ok=True)
MAX_SECONDS=int(os.getenv("MAX_VIDEO_SECONDS","7200"))
URL_ENABLED=os.getenv("URL_DOWNLOAD_ENABLED","true").lower()=="true"
MAX_UPLOAD=int(os.getenv("MAX_UPLOAD_BYTES",str(2*1024**3)))
DB_URL=os.getenv("DATABASE_URL","sqlite:////data/clipforge.db")
engine=create_engine(DB_URL,connect_args={"check_same_thread":False} if DB_URL.startswith("sqlite") else {})
Session=sessionmaker(engine,expire_on_commit=False)
celery=Celery("clipforge",broker=os.getenv("REDIS_URL","redis://redis:6379/0"),backend=os.getenv("REDIS_URL","redis://redis:6379/0"))
celery.conf.update(task_track_started=True,task_acks_late=True,worker_prefetch_multiplier=1,task_soft_time_limit=3600,task_time_limit=3900,result_expires=86400)

class Base(DeclarativeBase): pass
class Job(Base):
    __tablename__="jobs"
    id: Mapped[str]=mapped_column(String,primary_key=True)
    source: Mapped[str]=mapped_column(Text)
    title: Mapped[str]=mapped_column(String,default="Untitled video")
    status: Mapped[str]=mapped_column(String,default="queued")
    step: Mapped[str]=mapped_column(String,default="queued")
    progress: Mapped[int]=mapped_column(Integer,default=0)
    error: Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    source_path: Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    transcript_json: Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=lambda:datetime.now(timezone.utc))
class Clip(Base):
    __tablename__="clips"
    id: Mapped[str]=mapped_column(String,primary_key=True)
    job_id: Mapped[str]=mapped_column(ForeignKey("jobs.id",ondelete="CASCADE"),index=True)
    title: Mapped[str]=mapped_column(String)
    start: Mapped[float]=mapped_column(Float)
    end: Mapped[float]=mapped_column(Float)
    score: Mapped[int]=mapped_column(Integer)
    breakdown: Mapped[str]=mapped_column(Text)
    reason: Mapped[str]=mapped_column(Text)
    transcript: Mapped[str]=mapped_column(Text)
    output_path: Mapped[Optional[str]]=mapped_column(Text,nullable=True)
Base.metadata.create_all(engine)

app=FastAPI(title="ClipForge API",version="0.2.0")
app.add_middleware(CORSMiddleware,allow_origins=os.getenv("CORS_ORIGINS","http://localhost:3000").split(","),allow_credentials=False,allow_methods=["GET","POST","DELETE"],allow_headers=["*"])
API_TOKEN=os.getenv("API_TOKEN","").strip()
@app.middleware("http")
async def require_api_token(request:Request,call_next):
    # Keep health checks and interactive docs available; protect all media/job endpoints when configured.
    if API_TOKEN and request.url.path.startswith("/api/"):
        import hmac
        supplied=request.headers.get("authorization","")
        expected="Bearer "+API_TOKEN
        if not hmac.compare_digest(supplied,expected):
            return __import__("fastapi").responses.JSONResponse({"detail":"Missing or invalid API token."},status_code=401)
    return await call_next(request)
class CreateJob(BaseModel):
    url: HttpUrl
    title: str=Field(default="Untitled video",max_length=160)
    permission_confirmed: bool
class RenderOptions(BaseModel):
    start: float=Field(ge=0)
    end: float=Field(gt=0)
    aspect: str="9:16"
    caption_style: str="bold_pop"
    title: str | None=Field(default=None,max_length=160)
    transcript: str | None=Field(default=None,max_length=20000)

def run(args:list[str],timeout:int=1800)->str:
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout,check=False)
    if p.returncode: raise RuntimeError(f"{Path(args[0]).name} failed: {(p.stderr or p.stdout)[-1200:]}")
    return p.stdout

def job_dir(job_id:str)->Path:
    if not re.fullmatch(r"[0-9a-f-]{36}",job_id): raise ValueError("Invalid job id")
    path=(DATA/job_id).resolve()
    if DATA not in path.parents: raise ValueError("Invalid path")
    path.mkdir(parents=True,exist_ok=True); return path

def update_job(job_id,status,step,progress,error=None):
    with Session() as s:
        j=s.get(Job,job_id)
        if j: j.status,j.step,j.progress,j.error=status,step,max(0,min(100,progress)),error; s.commit()

def metadata_for(url:str)->dict:
    assert_public_url(url)
    info=json.loads(run(["yt-dlp","--no-warnings","--no-playlist","--dump-single-json","--skip-download",url],timeout=90))
    if info.get("is_live") or info.get("live_status") in ("is_live","is_upcoming"): raise ValueError("Live streams are not supported.")
    duration=info.get("duration")
    if not isinstance(duration,(int,float)): raise ValueError("Could not determine video duration.")
    if duration>MAX_SECONDS: raise ValueError(f"Video exceeds configured {MAX_SECONDS}-second limit.")
    return {"title":str(info.get("title") or "Untitled video")[:160],"thumbnail":info.get("thumbnail"),"duration":duration,"uploader":info.get("uploader")}

def validate_video(path:Path)->float:
    """Check media duration using ffprobe before spending time on transcription."""
    if not path.is_file() or path.stat().st_size < 1024:
        raise ValueError("The uploaded or downloaded file is empty or invalid.")
    try:
        result=json.loads(run(["ffprobe","-v","error","-show_entries","format=duration:format_name","-of","json",str(path)],timeout=30))
        duration=float(result["format"]["duration"])
    except (KeyError,TypeError,ValueError,json.JSONDecodeError,RuntimeError) as e:
        raise ValueError("The source is not a readable video/audio media file.") from e
    if not 0 < duration <= MAX_SECONDS:
        raise ValueError(f"Video duration must be between 0 and {MAX_SECONDS} seconds.")
    return duration

def assert_public_url(url:str):
    """Reject obvious local/private targets before passing a URL to yt-dlp."""
    from urllib.parse import urlsplit
    import ipaddress, socket
    parsed=urlsplit(url)
    if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTP or HTTPS video URL without embedded credentials.")
    host=parsed.hostname.rstrip(".").lower()
    if host in ("localhost",) or host.endswith((".localhost",".local",".internal")):
        raise ValueError("Local and internal hostnames are not allowed.")
    try:
        addresses=[ipaddress.ip_address(host)] if host else []
    except ValueError:
        try: addresses=[ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=="https" else 80),type=socket.SOCK_STREAM)]
        except OSError as e: raise ValueError("Could not resolve the public video hostname.") from e
    if not addresses or any(not a.is_global for a in addresses):
        raise ValueError("Private, loopback, and reserved IP addresses are not allowed.")

def transcribe(source:Path)->list[dict]:
    from faster_whisper import WhisperModel
    device=os.getenv("WHISPER_DEVICE","cpu")
    model=WhisperModel(os.getenv("WHISPER_MODEL","base"),device=device,compute_type=os.getenv("WHISPER_COMPUTE_TYPE","int8" if device=="cpu" else "float16"))
    segments,_=model.transcribe(str(source),word_timestamps=True,vad_filter=True)
    words=[]
    for seg in segments:
        if seg.words:
            words.extend({"text":w.word.strip(),"start":float(w.start),"end":float(w.end)} for w in seg.words if w.end>w.start)
        elif seg.end>seg.start: words.append({"text":seg.text.strip(),"start":float(seg.start),"end":float(seg.end)})
    if not words: raise ValueError("No speech detected. Try a video with clear dialogue.")
    return words

def make_ass(words:list[dict],start:float,end:float,path:Path,style="bold_pop"):
    header="""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,64,&H00FFFFFF,&H0000FFFF,&H00101010,&H99000000,1,0,1,4,1,5,60,60,100,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def ts(t):
        cs=int(max(0,t)*100); return f"{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}"
    events=[]
    for w in words:
        a,b=max(start,w["start"]),min(end,w["end"])
        if b<=a: continue
        txt=re.sub(r"[{}\\]","",w["text"])
        if style in ("bold_pop","karaoke","word_by_word"): txt="{\\c&H0058FAC8&\\b1}"+txt+"{\\rDefault}"
        events.append(f"Dialogue: 0,{ts(a-start)},{ts(b-start)},Default,,0,0,100,,{{\\an2}}{txt}")
    path.write_text(header+"\n".join(events)+"\n",encoding="utf-8")

def render(source:Path,output:Path,start:float,end:float,aspect:str,ass:Path):
    if end<=start or end-start>90: raise ValueError("Clip length must be between 0 and 90 seconds.")
    dims={"9:16":(1080,1920),"1:1":(1080,1080),"16:9":(1920,1080)}
    if aspect not in dims: raise ValueError("Unsupported aspect ratio.")
    w,h=dims[aspect]; ass_arg=ass.as_posix().replace(":","\\:")
    vf=f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},ass={ass_arg}"
    run(["ffmpeg","-hide_banner","-loglevel","error","-y","-ss",str(start),"-i",str(source),"-t",str(end-start),"-vf",vf,"-c:v","libx264","-preset",os.getenv("FFMPEG_PRESET","veryfast"),"-crf","19","-c:a","aac","-b:a","192k","-movflags","+faststart",str(output)],timeout=1800)
    if not output.is_file() or output.stat().st_size<1024: raise RuntimeError("FFmpeg did not produce a valid MP4.")

def export_subtitles(words:list[dict],start:float,end:float,directory:Path,clip_id:str):
    chosen=[w for w in words if w["end"]>start and w["start"]<end]
    groups=[chosen[i:i+8] for i in range(0,len(chosen),8)]
    def stamp(t,comma=False):
        ms=int(max(0,t)*1000); return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02}{',' if comma else '.'}{ms%1000:03}"
    for ext,comma in (("srt",True),("vtt",False)):
        lines=["WEBVTT"] if ext=="vtt" else []
        for i,g in enumerate(groups,1):
            if not g: continue
            a=max(start,g[0]["start"])-start; b=min(end,g[-1]["end"])-start
            if ext=="srt": lines.append(str(i))
            lines.extend([f"{stamp(a,comma)} --> {stamp(b,comma)}"," ".join(w["text"] for w in g),""])
        (directory/f"{clip_id}.{ext}").write_text("\n".join(lines),encoding="utf-8")

def distribute_caption_words(text:str,start:float,end:float)->list[dict]:
    tokens=text.split()
    if not tokens: return []
    span=max(0.1,end-start); step=span/len(tokens)
    return [{"text":token,"start":start+i*step,"end":start+(i+1)*step} for i,token in enumerate(tokens)]

@celery.task(bind=True,autoretry_for=(RuntimeError,OSError),retry_backoff=True,retry_kwargs={"max_retries":2})
def process_job(self,job_id:str):
    directory=job_dir(job_id)
    try:
        with Session() as s:
            j=s.get(Job,job_id)
            if not j: return
            source=j.source; source_path=Path(j.source_path) if j.source_path and Path(j.source_path).exists() else None
        if source_path is None:
            update_job(job_id,"downloading","download",5)
            if source.startswith("upload:"): source_path=Path(source[7:])
            else:
                if not URL_ENABLED: raise ValueError("URL downloads disabled; upload a video instead.")
                meta=metadata_for(source)
                with Session() as s: j=s.get(Job,job_id); j.title=meta["title"]; s.commit()
                try: run(["yt-dlp","--no-playlist","--no-warnings","-f","bv*+ba/b","--merge-output-format","mp4","-o",str(directory/"source.%(ext)s"),source],timeout=1800)
                except RuntimeError as e:
                    if any(x in str(e).lower() for x in ("private video","sign in","age-restricted","geo-restricted","not available","403","429")): raise ValueError("Source site blocked the download. Verify permission or upload an authorized local file.") from e
                    raise
                files=[p for p in directory.glob("source.*") if p.suffix.lower() in (".mp4",".mkv",".webm",".mov")]
                if not files: raise RuntimeError("No supported source video was downloaded.")
                source_path=files[0]
            if not source_path.exists(): raise ValueError("Source file is missing.")
        validate_video(source_path)
        with Session() as s:
            j=s.get(Job,job_id)
            if j:
                j.source_path=str(source_path)
                s.commit()
        update_job(job_id,"transcribing","transcribe",25)
        with Session() as s: j=s.get(Job,job_id); words=json.loads(j.transcript_json) if j and j.transcript_json else None
        words=words or transcribe(source_path)
        with Session() as s: j=s.get(Job,job_id); j.transcript_json=json.dumps(words); s.commit()
        update_job(job_id,"analyzing","analyze",50)
        candidates=segment_words(words,15,min(90,MAX_SECONDS))
        if not candidates: raise ValueError("Too little speech for a 15-second clip.")
        ranked=[]
        for c in candidates:
            score=score_moment(heuristic_signals(c["text"]))
            ranked.append({**c,**score,"title":c["text"][:70],"reason":"Heuristic estimate from hook, emotion, story, novelty, quotability, opinion, and CTA signals."})
        ranked.sort(key=lambda c:c["score"],reverse=True)
        with Session() as s:
            s.query(Clip).filter(Clip.job_id==job_id).delete()
            for c in ranked[:12]:
                s.add(Clip(id=str(uuid.uuid4()),job_id=job_id,title=c["title"],start=c["start"],end=c["end"],score=c["score"],breakdown=json.dumps(c["breakdown"]),reason=c["reason"],transcript=c["text"]))
            s.commit()
        update_job(job_id,"rendering","render",65)
        with Session() as s: clips=s.scalars(select(Clip).where(Clip.job_id==job_id).order_by(Clip.score.desc()).limit(12)).all()
        outdir=directory/"clips"; outdir.mkdir(exist_ok=True)
        for i,c in enumerate(clips):
            out=outdir/f"{c.id}.mp4"; ass=outdir/f"{c.id}.ass"
            if not out.exists():
                make_ass(words,c.start,c.end,ass)
                render(source_path,out,c.start,c.end,os.getenv("DEFAULT_ASPECT","9:16"),ass)
                export_subtitles(words,c.start,c.end,outdir,c.id)
            with Session() as s: row=s.get(Clip,c.id); row.output_path=str(out); s.commit()
            update_job(job_id,"rendering","render",65+int(34*(i+1)/max(1,len(clips))))
        update_job(job_id,"completed","completed",100)
    except ValueError as e:
        update_job(job_id,"failed","failed",0,str(e)[:1000]); log.warning("job %s rejected: %s",job_id,e)
    except RuntimeError as e:
        log.exception("job %s transient processing error",job_id)
        if self.request.retries < 2:
            update_job(job_id,"retrying","retrying",5,"Temporary processing error; retrying automatically.")
            raise
        update_job(job_id,"failed","failed",0,"Processing failed after automatic retries. Check worker logs and retry the project.")
    except Exception:
        log.exception("job %s failed",job_id)
        update_job(job_id,"failed","failed",0,"Processing failed. Check worker logs and retry; the source site may block downloads.")

@app.get("/health")
def health():
    try:
        with Session() as s: s.execute(select(Job.id).limit(1)).all()
        return {"status":"ok","database":"ok","queue":"celery/redis","url_download_enabled":URL_ENABLED}
    except Exception: return {"status":"degraded","database":"unavailable"}

@app.post("/api/metadata")
def metadata(body:CreateJob):
    if not body.permission_confirmed: raise HTTPException(400,"Only use videos you own or have permission to repurpose.")
    if not URL_ENABLED: raise HTTPException(403,"URL downloads are disabled.")
    try: assert_public_url(str(body.url)); return metadata_for(str(body.url))
    except ValueError as e: raise HTTPException(400,str(e))
    except Exception as e: raise HTTPException(422,"Video is private, blocked, unsupported, or unavailable.") from e

def enqueue(source:str,title:str):
    job_id=str(uuid.uuid4())
    with Session() as s: s.add(Job(id=job_id,source=source,title=title,status="queued",step="queued",progress=0)); s.commit()
    task=process_job.delay(job_id)
    return {"id":job_id,"task_id":task.id,"title":title,"status":"queued","progress":0}

@app.post("/api/jobs")
def create_job(body:CreateJob):
    if not body.permission_confirmed: raise HTTPException(400,"Permission confirmation required.")
    if not URL_ENABLED: raise HTTPException(403,"URL downloads are disabled; upload a video instead.")
    try: assert_public_url(str(body.url))
    except ValueError as e: raise HTTPException(400,str(e))
    return enqueue(str(body.url),body.title)

@app.post("/api/uploads")
async def upload(file:UploadFile=File(...),permission_confirmed:bool=Form(...)):
    if not permission_confirmed: raise HTTPException(400,"Permission confirmation required.")
    ext=Path(file.filename or "").suffix.lower()
    if ext not in (".mp4",".mov",".mkv",".webm"): raise HTTPException(415,"Use MP4, MOV, MKV, or WebM.")
    job_id=str(uuid.uuid4()); directory=job_dir(job_id)/"upload"; directory.mkdir(exist_ok=True)
    target=directory/f"source{ext}"; total=0
    try:
        with target.open("wb") as f:
            while True:
                chunk=await file.read(1024*1024)
                if not chunk: break
                total+=len(chunk)
                if total>MAX_UPLOAD: raise HTTPException(413,"Upload exceeds configured size limit.")
                f.write(chunk)
    except Exception:
        target.unlink(missing_ok=True); raise
    with Session() as s: s.add(Job(id=job_id,source="upload:"+str(target),source_path=str(target),title=Path(file.filename or "video").stem[:160])); s.commit()
    task=process_job.delay(job_id)
    return {"id":job_id,"task_id":task.id,"title":Path(file.filename or "video").stem,"status":"queued","progress":0}

@app.get("/api/jobs")
def list_jobs():
    with Session() as s:
        jobs=s.scalars(select(Job).order_by(Job.created_at.desc()).limit(100)).all()
        return {"jobs":[{"id":j.id,"title":j.title,"status":j.status,"step":j.step,"progress":j.progress,"error":j.error,"created_at":j.created_at.isoformat()} for j in jobs]}

@app.get("/api/jobs/{job_id}")
def get_job(job_id:str):
    with Session() as s:
        j=s.get(Job,job_id)
        if not j: raise HTTPException(404,"Project not found.")
        clips=s.scalars(select(Clip).where(Clip.job_id==job_id).order_by(Clip.score.desc())).all()
        return {"id":j.id,"title":j.title,"status":j.status,"step":j.step,"progress":j.progress,"error":j.error,"created_at":j.created_at.isoformat(),"clips":[{"id":c.id,"title":c.title,"start":c.start,"end":c.end,"duration":c.end-c.start,"score":c.score,"breakdown":json.loads(c.breakdown),"reason":c.reason,"transcript":c.transcript,"download_url":f"/api/clips/{c.id}/download" if c.output_path and Path(c.output_path).is_file() else None,"srt_url":f"/api/clips/{c.id}/subtitles/srt" if c.output_path and Path(c.output_path).with_suffix('.srt').is_file() else None,"vtt_url":f"/api/clips/{c.id}/subtitles/vtt" if c.output_path and Path(c.output_path).with_suffix('.vtt').is_file() else None} for c in clips]}

@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id:str):
    with Session() as s: c=s.get(Clip,clip_id)
    if not c or not c.output_path or not Path(c.output_path).is_file(): raise HTTPException(404,"Clip is not ready.")
    return FileResponse(c.output_path,media_type="video/mp4",filename=f"clipforge-{clip_id}.mp4")

@app.post("/api/clips/{clip_id}/render")
def rerender_clip(clip_id:str,options:RenderOptions):
    if options.end<=options.start or options.end-options.start>90:
        raise HTTPException(400,"Clip length must be between 0 and 90 seconds.")
    if options.aspect not in ("9:16","1:1","16:9"):
        raise HTTPException(400,"Aspect must be 9:16, 1:1 or 16:9.")
    if options.caption_style not in ("bold_pop","karaoke","word_by_word","minimal","boxed","outline"):
        raise HTTPException(400,"Unsupported caption style.")
    with Session() as s:
        c=s.get(Clip,clip_id)
        if not c: raise HTTPException(404,"Clip not found.")
        j=s.get(Job,c.job_id)
        if not j or not j.source_path or not Path(j.source_path).is_file(): raise HTTPException(409,"Original source file is unavailable.")
        source=Path(j.source_path); title=(options.title or c.title).strip()[:160] or c.title; transcript=c.transcript if options.transcript is None else options.transcript; job_id=c.job_id
    try: duration=validate_video(source)
    except ValueError as e: raise HTTPException(422,str(e)) from e
    if options.end>duration: raise HTTPException(400,"Clip end exceeds source duration.")
    words=distribute_caption_words(transcript,options.start,options.end)
    directory=job_dir(job_id)/"clips"; directory.mkdir(exist_ok=True)
    output=directory/f"{clip_id}.mp4"; ass=directory/f"{clip_id}.ass"
    try:
        make_ass(words,options.start,options.end,ass,options.caption_style)
        render(source,output,options.start,options.end,options.aspect,ass)
        export_subtitles(words,options.start,options.end,directory,clip_id)
    except (ValueError,RuntimeError,OSError) as e: raise HTTPException(422,f"Render failed: {str(e)[:500]}") from e
    with Session() as s:
        c=s.get(Clip,clip_id); c.start=options.start; c.end=options.end; c.output_path=str(output); c.title=title; c.transcript=transcript; s.commit()
    return {"id":clip_id,"title":title,"start":options.start,"end":options.end,"duration":options.end-options.start,"download_url":f"/api/clips/{clip_id}/download","srt_url":f"/api/clips/{clip_id}/subtitles/srt","vtt_url":f"/api/clips/{clip_id}/subtitles/vtt"}

@app.get("/api/clips/{clip_id}/subtitles/{extension}")
def download_subtitles(clip_id:str,extension:str):
    if extension not in ("srt","vtt"): raise HTTPException(404,"Subtitle format not found.")
    with Session() as s: c=s.get(Clip,clip_id)
    if not c or not c.output_path: raise HTTPException(404,"Clip not found.")
    path=Path(c.output_path).with_suffix("."+extension)
    if not path.is_file(): raise HTTPException(404,"Subtitle file is not ready.")
    return FileResponse(path,media_type="text/vtt" if extension=="vtt" else "application/x-subrip",filename=f"clipforge-{clip_id}.{extension}")

@app.post("/api/jobs/{job_id}/retry")
def retry(job_id:str):
    with Session() as s:
        j=s.get(Job,job_id)
        if not j: raise HTTPException(404,"Project not found.")
        if j.status!="failed": raise HTTPException(409,"Only failed jobs can be retried.")
        j.status,j.step,j.progress,j.error="queued","queued",0,None; s.commit()
    task=process_job.delay(job_id); return {"id":job_id,"task_id":task.id,"status":"queued"}

@app.delete("/api/jobs/{job_id}")
def delete_job(job_id:str):
    with Session() as s:
        j=s.get(Job,job_id)
        if not j: raise HTTPException(404,"Project not found.")
        s.query(Clip).filter(Clip.job_id==job_id).delete(); s.delete(j); s.commit()
    shutil.rmtree(DATA/job_id,ignore_errors=True)
    return {"id":job_id,"deleted":True}

@app.get("/api/jobs/{job_id}/events")
async def events(job_id:str):
    import asyncio
    async def stream():
        for _ in range(600):
            with Session() as s:
                j=s.get(Job,job_id)
                if not j:
                    yield 'event: error\ndata: {"error":"Project not found"}\n\n'; return
                payload=json.dumps({"id":j.id,"status":j.status,"step":j.step,"progress":j.progress,"error":j.error})
            yield f"data: {payload}\n\n"
            if j.status in ("completed","failed"): return
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
