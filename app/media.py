import hashlib, json, os, shutil, subprocess, time
from pathlib import Path
from .settings import ALLOWED_ROOT
class Interrupted(Exception): pass

def local_path(value,exists=True):
    p=Path(value).expanduser().resolve()
    if not p.is_relative_to(ALLOWED_ROOT): raise ValueError('Path must be inside MEDIA_ROOT: '+str(ALLOWED_ROOT))
    if exists and not p.exists(): raise ValueError('Path does not exist')
    return p

def probe(path):
    try:
        result=subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)],check=True,capture_output=True,text=True,timeout=30)
        data=json.loads(result.stdout)
        v=next(s for s in data['streams'] if s['codec_type']=='video')
        duration=float(data['format']['duration'])
        if not 0<duration<86400: raise ValueError('Video must be less than 24 hours')
        return {'duration':duration,'width':v['width'],'height':v['height'],'audio':any(s['codec_type']=='audio' for s in data['streams'])}
    except (subprocess.SubprocessError,KeyError,StopIteration,ValueError) as e: raise ValueError('Cannot read this video with ffprobe') from e

def checksum(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def encode(source,target,start,duration,framing,check,progress):
    target=Path(target); target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix('.partial.mp4'); log=target.with_suffix('.ffmpeg.log'); pf=target.with_suffix('.progress')
    filt='scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920' if framing=='crop' else 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black'
    cmd=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-ss',str(start),'-i',str(source),'-t',str(duration),'-map','0:v:0','-map','0:a:0?','-vf',filt+',setsar=1,fps=30','-c:v','libx264','-threads','2','-preset','fast','-crf','23','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-movflags','+faststart','-progress',str(pf),str(temp)]
    if shutil.disk_usage(target.parent).free < 128*1024*1024: raise ValueError('Insufficient free disk space')
    started=time.monotonic()
    with log.open('w') as err:
        proc=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=err)
        try:
            while proc.poll() is None:
                check()
                if time.monotonic()-started>7200: raise ValueError('Clip encoding timed out')
                if pf.exists():
                    values=[l.split('=',1)[1] for l in pf.read_text().splitlines() if l.startswith('out_time_us=')]
                    if values:
                        try: progress(min(.99,float(values[-1])/1e6/duration))
                        except ValueError: pass
                time.sleep(.4)
            if proc.returncode: raise ValueError('FFmpeg failed; inspect '+str(log))
            check(); info=probe(temp)
            if (info['width'],info['height'])!=(1080,1920) or abs(info['duration']-duration)>.25: raise ValueError('Clip failed output validation')
            temp.replace(target)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(5)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            temp.unlink(missing_ok=True); pf.unlink(missing_ok=True)
    # Successful clips need no diagnostic sidecar; retain logs on failure.
    log.unlink(missing_ok=True)
    return checksum(target)
