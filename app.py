import os, uuid, json, time, shutil, subprocess, threading, re
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from openai import OpenAI

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / 'uploads'; OUTPUT_DIR = BASE / 'outputs'; WORK_DIR = BASE / 'work'
for p in (UPLOAD_DIR, OUTPUT_DIR, WORK_DIR): p.mkdir(exist_ok=True)
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024
ALLOWED_EXTENSIONS = {'mp4','mov','avi','mkv','webm','m4v'}
jobs = {}

def api_client():
    key = os.environ.get('OPENAI_API_KEY','').strip()
    if not key: raise RuntimeError('Chưa cấu hình OPENAI_API_KEY trong Render Environment.')
    return OpenAI(api_key=key)

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode: raise RuntimeError((p.stderr or 'Lệnh xử lý video thất bại')[-1800:])
    return p.stdout.strip()

def set_job(job_id, **kw):
    jobs[job_id].update(kw)

def timestamp(t):
    t=max(0,float(t)); h=int(t//3600); m=int((t%3600)//60); s=int(t%60); ms=int(round((t-int(t))*1000))
    if ms==1000: s+=1; ms=0
    return f'{h:02}:{m:02}:{s:02},{ms:03}'

def process_video(job_id, settings):
    job=jobs[job_id]; src=UPLOAD_DIR/job['stored_name']; work=WORK_DIR/job_id; work.mkdir(exist_ok=True)
    try:
        if not shutil.which('ffmpeg'): raise RuntimeError('Máy chủ chưa cài FFmpeg. Kiểm tra render.yaml rồi deploy lại.')
        client=api_client(); set_job(job_id,status='processing',progress=3,message='Đang tách âm thanh khỏi video…')
        audio=work/'audio.mp3'
        run(['ffmpeg','-y','-i',str(src),'-vn','-ac','1','-ar','16000','-b:a','48k',str(audio)])
        # Split audio into 8-minute chunks to stay below transcription upload limits.
        chunks=[]
        run(['ffmpeg','-y','-i',str(audio),'-f','segment','-segment_time','480','-c','copy',str(work/'chunk_%03d.mp3')])
        chunks=sorted(work.glob('chunk_*.mp3'))
        segments=[]
        for i,ch in enumerate(chunks):
            set_job(job_id,progress=min(10+int(30*(i+1)/max(1,len(chunks))),40),message=f'AI đang nhận diện lời thoại ({i+1}/{len(chunks)})…')
            with open(ch,'rb') as f:
                tr=client.audio.transcriptions.create(model='whisper-1',file=f,response_format='verbose_json',timestamp_granularities=['segment'])
            offset=i*480
            for seg in (getattr(tr,'segments',None) or []):
                txt=(seg.text or '').strip()
                if txt: segments.append({'start':float(seg.start)+offset,'end':float(seg.end)+offset,'text':txt})
        if not segments: raise RuntimeError('AI không nhận diện được lời thoại trong video.')
        set_job(job_id,progress=45,message=f'Đã nhận diện {len(segments)} đoạn lời thoại. Đang dịch…')
        # Translate in manageable batches to preserve alignment.
        translated=[]
        target=settings.get('target_lang','Tiếng Việt')
        for start in range(0,len(segments),40):
            batch=segments[start:start+40]
            prompt='Translate each speech segment into '+target+'. Return a JSON object with key translations containing an array of strings, same length and order. Keep meaning natural and concise for subtitles. Input: '+json.dumps([x['text'] for x in batch],ensure_ascii=False)
            resp=client.chat.completions.create(model='gpt-4o-mini',messages=[{'role':'system','content':'You are a professional subtitle translator. Output valid JSON only.'},{'role':'user','content':prompt}],response_format={'type':'json_object'})
            raw=resp.choices[0].message.content or '{}'
            try:
                obj=json.loads(raw); arr=obj.get('translations') or obj.get('translated') or obj.get('items')
                if arr is None:
                    # Retry format with a named JSON property.
                    resp=client.chat.completions.create(model='gpt-4o-mini',messages=[{'role':'system','content':'Return JSON object with key translations containing an array of translated strings, one per input, same order.'},{'role':'user','content':prompt}],response_format={'type':'json_object'})
                    arr=json.loads(resp.choices[0].message.content or '{}').get('translations')
                if not isinstance(arr,list) or len(arr)!=len(batch): raise ValueError('translation array length mismatch')
            except Exception as e: raise RuntimeError('Không đọc được kết quả dịch từ AI: '+str(e))
            translated.extend(str(x) for x in arr)
            set_job(job_id,progress=min(45+int(20*len(translated)/len(segments)),65),message=f'Đang dịch lời thoại ({len(translated)}/{len(segments)})…')
        mode=settings.get('subtitle_mode','Phụ đề + lồng tiếng')
        want_sub='Chỉ lồng tiếng' not in mode
        want_dub='Chỉ phụ đề' not in mode
        srt=work/'translated.srt'
        with open(srt,'w',encoding='utf-8') as f:
            for i,(seg,txt) in enumerate(zip(segments,translated),1):
                f.write(f"{i}\n{timestamp(seg['start'])} --> {timestamp(max(seg['start']+0.25,seg['end']))}\n{txt}\n\n")
        output=OUTPUT_DIR/f'{job_id}_vietsub.mp4'
        quality=settings.get('quality','1080p'); scale={'720p':720,'1080p':1080,'4K':2160}.get(quality,1080)
        original_volume=max(0,min(100,int(settings.get('original_volume',70))))/100
        dub_volume=max(0,min(100,int(settings.get('dub_volume',100))))/100
        set_job(job_id,progress=68,message='Đang dựng video và phụ đề…')
        if want_dub:
            # Create one speech clip per subtitle segment, then place each at its source timestamp.
            voice_choice=settings.get('voice','Nữ — tự nhiên')
            voice='nova' if 'Nữ' in voice_choice else 'onyx'
            speech=[]
            for i,(seg,txt) in enumerate(zip(segments,translated)):
                if not txt.strip(): continue
                out=work/f'voice_{i:04}.mp3'
                try:
                    with client.audio.speech.with_streaming_response.create(model='gpt-4o-mini-tts',voice=voice,input=txt[:3500],response_format='mp3') as response: response.stream_to_file(out)
                except Exception as e: raise RuntimeError('Tạo giọng đọc AI thất bại: '+str(e))
                speech.append((seg,out))
                set_job(job_id,progress=min(68+int(22*(i+1)/len(segments)),90),message=f'Đang tạo giọng đọc AI ({i+1}/{len(segments)})…')
            if speech:
                inputs=[]
                for seg,p in speech: inputs += ['-i',str(p)]
                filters=[]
                for i,(seg,p) in enumerate(speech): filters.append(f'[{i+1}:a]adelay={int(seg["start"]*1000)}|{int(seg["start"]*1000)},volume={dub_volume}[a{i}]')
                labels=''.join(f'[a{i}]' for i in range(len(speech)))
                filters.append(f'{labels}amix=inputs={len(speech)}:duration=longest:normalize=0[dub]')
                fc=';'.join(filters)
                # subtitle burn-in uses escaped absolute path for FFmpeg filter syntax
                subtitle_filter=''
                if want_sub:
                    sp=str(srt.resolve()).replace('\\','/').replace(':','\\:').replace("'","\\'")
                    subtitle_filter=f",subtitles='{sp}'"
                vf=f"scale=-2:'min({scale},ih)':force_original_aspect_ratio=decrease{subtitle_filter}"
                cmd=['ffmpeg','-y','-i',str(src),*inputs,'-filter_complex',fc,'-map','0:v:0','-map','0:a:0?','-map','[dub]','-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','23','-filter:a:0',f'volume={original_volume}','-c:a','aac','-shortest',str(output)]
                # Mix original and dubbed tracks together, retaining original audio at chosen volume.
                filters[-1]=f'{labels}amix=inputs={len(speech)}:duration=longest:normalize=0[dub]'
                fc=';'.join(filters)+f';[0:a]volume={original_volume}[orig];[orig][dub]amix=inputs=2:duration=first:normalize=0[mix]'
                cmd=['ffmpeg','-y','-i',str(src),*inputs,'-filter_complex',fc,'-map','0:v:0','-map','[mix]','-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','23','-c:a','aac','-b:a','192k','-shortest',str(output)]
                run(cmd)
            else:
                want_dub=False
        if not want_dub:
            subtitle_filter=''
            if want_sub:
                sp=str(srt.resolve()).replace('\\','/').replace(':','\\:').replace("'","\\'")
                subtitle_filter=f",subtitles='{sp}'"
            vf=f"scale=-2:'min({scale},ih)':force_original_aspect_ratio=decrease{subtitle_filter}"
            run(['ffmpeg','-y','-i',str(src),'-vf',vf,'-af',f'volume={original_volume}','-c:v','libx264','-preset','veryfast','-crf','23','-c:a','aac','-b:a','192k','-movflags','+faststart',str(output)])
        # Always expose SRT separately for compatibility.
        srt_out=OUTPUT_DIR/f'{job_id}_subtitles.srt'; shutil.copy2(srt,srt_out)
        set_job(job_id,status='completed',progress=100,message='Hoàn tất! Video đã xử lý xong.',output=f'/outputs/{output.name}',output_url=f'/outputs/{output.name}',subtitle_url=f'/outputs/{srt_out.name}',finished_at=int(time.time()))
    except Exception as e:
        set_job(job_id,status='failed',progress=0,message=str(e),error=str(e))

@app.after_request
def cors(response):
    response.headers['Access-Control-Allow-Origin']='*'
    response.headers['Access-Control-Allow-Methods']='GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers']='Content-Type'
    return response

@app.get('/')
def index(): return render_template('index.html')

@app.get('/health')
def health():
    return jsonify({'ok':True,'service':'VietSub AI','openai_api_key_configured':bool(os.environ.get('OPENAI_API_KEY','').strip()),'translation_engine':'openai-whisper-gpt-4o-mini-tts','ffmpeg_available':bool(shutil.which('ffmpeg'))})

@app.post('/api/upload')
def upload_video():
    if 'video' not in request.files: return jsonify({'error':'Chưa nhận được file video.'}),400
    file=request.files['video']
    if not file.filename: return jsonify({'error':'Hãy chọn video cần dịch.'}),400
    ext=file.filename.rsplit('.',1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS: return jsonify({'error':'Định dạng chưa hỗ trợ. Dùng MP4, MOV, AVI, MKV, WEBM hoặc M4V.'}),400
    job_id=uuid.uuid4().hex[:12]; filename=secure_filename(file.filename) or f'video.{ext}'; stored=f'{job_id}_{filename}'; path=UPLOAD_DIR/stored; file.save(path)
    jobs[job_id]={'id':job_id,'filename':filename,'stored_name':stored,'size':path.stat().st_size,'status':'uploaded','progress':0,'message':'Video đã tải lên.','created_at':int(time.time()),'output':None}
    return jsonify(jobs[job_id]),201

@app.post('/api/jobs/<job_id>/start')
def start_job(job_id):
    job=jobs.get(job_id)
    if not job: return jsonify({'error':'Không tìm thấy video. Hãy tải lên lại.'}),404
    if job.get('status')=='processing': return jsonify(job),202
    if job.get('status')=='completed': return jsonify(job),200
    settings=request.get_json(silent=True) or {}
    job['settings']=settings; job['status']='queued'; job['progress']=1; job['message']='Đã đưa video vào hàng xử lý.'
    threading.Thread(target=process_video,args=(job_id,settings),daemon=True).start()
    return jsonify(job),202

@app.get('/api/jobs/<job_id>')
def get_job(job_id):
    job=jobs.get(job_id)
    if not job: return jsonify({'error':'Không tìm thấy tác vụ. Có thể server vừa khởi động lại.'}),404
    return jsonify(job)

@app.get('/api/album')
def album():
    items=[]
    for p in sorted(OUTPUT_DIR.iterdir(),key=lambda x:x.stat().st_mtime,reverse=True):
        if p.is_file() and p.suffix.lower() in {'.mp4','.srt'}: items.append({'name':p.name,'url':f'/outputs/{p.name}','size':p.stat().st_size})
    return jsonify(items)

@app.get('/outputs/<path:filename>')
def output_file(filename): return send_from_directory(OUTPUT_DIR,filename,as_attachment=True)
@app.errorhandler(413)
def too_large(_): return jsonify({'error':'Video vượt giới hạn upload của máy chủ.'}),413
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')))
