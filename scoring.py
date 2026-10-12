"""Transparent heuristic scoring and transcript segmentation."""
import re
WEIGHTS={"hook":.22,"emotion":.16,"story":.16,"novelty":.14,"quotability":.12,"opinion":.10,"call_to_action":.10}

def score_moment(signals:dict,audio_adjustment:float=0)->dict:
    for key in WEIGHTS:
        value=signals.get(key,0)
        if not isinstance(value,(int,float)) or not 0<=value<=100: raise ValueError(f"{key} must be between 0 and 100")
    breakdown={key:round(signals.get(key,0)*weight) for key,weight in WEIGHTS.items()}
    raw=sum(signals.get(key,0)*weight for key,weight in WEIGHTS.items())
    return {"score":max(0,min(100,round(raw+max(-5,min(5,float(audio_adjustment)))))),"breakdown":breakdown}

def segment_words(words:list[dict],min_seconds:float=15,max_seconds:float=90)->list[dict]:
    if min_seconds<=0 or max_seconds<min_seconds: raise ValueError("Invalid duration bounds")
    valid=sorted((w for w in words if isinstance(w.get("start"),(int,float)) and isinstance(w.get("end"),(int,float)) and w["start"]>=0 and w["end"]>w["start"] and str(w.get("text"," ")).strip()),key=lambda w:w["start"])
    result=[]; cursor=0
    while cursor<len(valid):
        start=valid[cursor]["start"]; hard=cursor
        while hard<len(valid) and valid[hard]["end"]-start<=max_seconds: hard+=1
        if hard==cursor: cursor+=1; continue
        boundary=hard
        if hard<len(valid):
            for i in range(hard-1,cursor,-1):
                if valid[i]["end"]-start>=min_seconds and re.search(r'[.!?]["\')\]]?$',valid[i]["text"].strip()): boundary=i+1; break
        chosen=valid[cursor:boundary]
        if chosen and chosen[-1]["end"]-chosen[0]["start"]>=min_seconds:
            result.append({"start":chosen[0]["start"],"end":chosen[-1]["end"],"text":" ".join(w["text"] for w in chosen),"word_count":len(chosen)}); cursor=boundary
        else: cursor=max(cursor+1,boundary)
    return result

def heuristic_signals(text:str)->dict:
    words=re.findall(r"\b[\w']+\b",text.lower())
    def cue(terms): return min(100,35+12*sum(w in terms for w in words))
    return {"hook":cue({"how","why","secret","mistake","never","truth","imagine","stop","best"}),"emotion":cue({"love","hate","shocking","amazing","fear","excited","terrible","incredible"}),"story":min(95,35+min(len(words),120)*.5),"novelty":min(85,40+len(set(words))/max(1,len(words))*40),"quotability":min(90,35+text.count("?")*8+text.count("!")*5),"opinion":cue({"think","believe","wrong","right","should","must","opinion"}),"call_to_action":min(90,35+12*sum(w in words for w in ("try","learn","follow","start","remember")))}
