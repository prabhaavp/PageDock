import fcntl, io, json, os, re, secrets, shutil, socket, subprocess, threading, time, uuid
import xml.etree.ElementTree as ET
from functools import wraps
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse
import requests
from flask import Flask, abort, jsonify, request, send_file, session, render_template_string
from PIL import Image

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.environ['SESSION_SECRET']
app.config.update(PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=25 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_SECURE=os.getenv('SECURE_COOKIE') == '1')
PASSWORD = os.environ['APP_PASSWORD']
PUBLIC_ORIGIN = os.getenv('PUBLIC_ORIGIN', '').rstrip('/')
if len(PASSWORD) < (20 if PUBLIC_ORIGIN else 12) or len(app.secret_key) < 32:
    raise RuntimeError('APP_PASSWORD must be at least 12 characters (20 for public access), and SESSION_SECRET at least 32 characters')
if PUBLIC_ORIGIN:
    parsed_origin=urlparse(PUBLIC_ORIGIN)
    if parsed_origin.scheme!='https' or not parsed_origin.hostname or parsed_origin.path or parsed_origin.query or parsed_origin.fragment or parsed_origin.username or parsed_origin.password:
        raise RuntimeError('PUBLIC_ORIGIN must be an HTTPS origin without a path')
    if os.getenv('SECURE_COOKIE')!='1': raise RuntimeError('Public access requires SECURE_COOKIE=1')
    app.config['TRUSTED_HOSTS']=[parsed_origin.hostname]

APP_NAME = os.getenv('APP_NAME', 'Document Studio').strip() or 'Document Studio'
PRINTER_NAME = os.getenv('PRINTER_NAME', 'Printer').strip() or 'Printer'
QUEUE = os.getenv('CUPS_QUEUE', 'printer')
if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', QUEUE): raise RuntimeError('Invalid CUPS_QUEUE')
UNDO_DEPTH = int(os.getenv('UNDO_DEPTH', '3'))
if not 1 <= UNDO_DEPTH <= 10: raise RuntimeError('UNDO_DEPTH must be between 1 and 10')
PRINTER = os.environ['PRINTER_HOST']
if not all(c.isalnum() or c in '.-:' for c in PRINTER):
    raise RuntimeError('Invalid printer host')
BASE = f'http://{PRINTER}'
DATA = Path(os.getenv('DATA_DIR', '/data/app')); DATA.mkdir(mode=0o700, exist_ok=True)
SESSIONS = DATA / 'sessions'; SESSIONS.mkdir(mode=0o700, exist_ok=True)
PAGES = DATA / 'pages'; PAGES.mkdir(mode=0o700, exist_ok=True)
PRINTS = DATA / 'prints'; PRINTS.mkdir(mode=0o700, exist_ok=True)
DOCUMENTS = DATA / 'documents'; DOCUMENTS.mkdir(mode=0o700, exist_ok=True)
SETTINGS = DATA / 'settings.json'
def settings():
    try:
        value = json.loads(SETTINGS.read_text()); days = int(value.get('retention_days', 30))
        return {'retention_days': max(1, min(365, days))}
    except (OSError, ValueError, TypeError): return {'retention_days': 30}
def cleanup():
    session_cutoff=time.time()-12*3600
    for marker in SESSIONS.glob('*/.active'):
        try:
            if marker.stat().st_mtime < session_cutoff:
                marker.unlink(missing_ok=True); marker.parent.rmdir()
        except FileNotFoundError: pass
    cutoff = time.time() - settings()['retention_days'] * 86400
    for old in PAGES.glob('*.undo*'):
        try:
            suffix=old.name.rsplit('.undo',1)[1]
            if suffix.isdigit() and int(suffix)>UNDO_DEPTH: old.unlink(missing_ok=True)
        except FileNotFoundError: pass
    for work in PAGES.glob('*.working'):
        try:
            if work.stat().st_mtime < time.time()-3600: work.unlink(missing_ok=True)
        except FileNotFoundError: pass
    for root, suffix in ((PAGES, '.dpi'), (PRINTS, '.json'), (DOCUMENTS, '.json')):
        for meta in root.glob('*' + suffix):
            try:
                if meta.stat().st_mtime < cutoff:
                    for path in root.glob(meta.stem + '.*'): path.unlink(missing_ok=True)
            except FileNotFoundError: pass
def cleanup_loop():
    while True:
        try: cleanup()
        except OSError: app.logger.exception('Could not clean up expired files')
        time.sleep(3600)
threading.Thread(target=cleanup_loop, daemon=True).start()
SIZES = {'letter': (2550, 3300), 'a4': (2480, 3508), 'photo': (1200, 1800), 'full': (2550, 3508)}

@app.before_request
def protect():
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('Origin')
        if origin and (origin != PUBLIC_ORIGIN if PUBLIC_ORIGIN else urlparse(origin).netloc != request.host): abort(403)
        if request.path != '/api/login' and request.headers.get('X-CSRF-Token') != session.get('csrf'): abort(403)

def auth(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not authenticated(): abort(401)
        return f(*a, **kw)
    return wrapped

def authenticated():
    token = session.get('workspace', '')
    return bool(session.get('authenticated') and len(token) == 32 and all(c in '0123456789abcdef' for c in token) and (SESSIONS / token / '.active').is_file())

def folder():
    if not authenticated(): abort(401)
    return PAGES

@app.errorhandler(Exception)
def errors(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException): return jsonify(error=e.description), e.code
    app.logger.exception('Request failed')
    return jsonify(error='The operation failed. Check the printer connection and application logs.'), 500

@app.after_request
def no_sensitive_cache(response):
    if request.path.startswith('/api/'):
        response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    if PUBLIC_ORIGIN: response.headers['Strict-Transport-Security']='max-age=31536000'
    return response

@app.get('/')
def index():
    return render_template_string((Path(app.static_folder)/'index.html').read_text(),app_name=APP_NAME,printer_name=PRINTER_NAME)

@app.get('/manifest.webmanifest')
def manifest():
    response=jsonify({'id':'/','name':APP_NAME,'short_name':APP_NAME[:32],'description':'Scan and print documents.','start_url':'/','scope':'/','display':'standalone','background_color':'#111713','theme_color':'#111713','icons':[{'src':'/icons/icon-192.png','sizes':'192x192','type':'image/png','purpose':'any'},{'src':'/icons/icon-512.png','sizes':'512x512','type':'image/png','purpose':'any'},{'src':'/icons/icon-maskable-512.png','sizes':'512x512','type':'image/png','purpose':'maskable'}]})
    response.mimetype='application/manifest+json';return response

@app.post('/api/login')
def login():
    # Share a bounded login throttle between Gunicorn workers.
    with (SESSIONS / 'login-attempts.json').open('a+') as attempts:
        fcntl.flock(attempts, fcntl.LOCK_EX)
        attempts.seek(0)
        try: history = json.load(attempts)
        except (ValueError, json.JSONDecodeError): history = {}
        now = time.time()
        history = {ip: [t for t in times if now - t < 300] for ip, times in history.items()}
        history = {ip: times for ip, times in history.items() if times}
        address = request.remote_addr or 'unknown'
        recent = history.get(address, [])
        total = sum(len(times) for times in history.values())
        if len(recent) >= 5 or total >= 50: abort(429, 'Too many attempts. Try again in five minutes.')
        supplied = str((request.get_json(silent=True) or {}).get('password', ''))
        valid = secrets.compare_digest(supplied.encode('utf-8'), PASSWORD.encode('utf-8'))
        if valid: history.pop(address, None)
        else: history[address] = recent + [now]
        attempts.seek(0); attempts.truncate(); json.dump(history, attempts); attempts.flush()
    if not valid: abort(401, 'Incorrect password')
    session.clear(); session.update(authenticated=True, csrf=secrets.token_urlsafe(32), workspace=uuid.uuid4().hex)
    session.permanent = True
    work = SESSIONS / session['workspace']; work.mkdir(mode=0o700); (work / '.active').touch(mode=0o600)
    return jsonify(csrf=session['csrf'])

@app.get('/api/session')
def current(): return jsonify(authenticated=authenticated(), csrf=session.get('csrf'))

@app.post('/api/logout')
@auth
def logout():
    work = SESSIONS / session['workspace']
    for p in work.glob('*'): p.unlink(missing_ok=True)
    work.rmdir(); session.clear(); return jsonify(ok=True)

@app.get('/api/status')
@auth
def status():
    try:
        r = requests.get(BASE + '/eSCL/ScannerStatus', timeout=4); r.raise_for_status()
        import xml.etree.ElementTree as ET
        root=ET.fromstring(r.content)
        state=next((x.text for x in root.iter() if x.tag.endswith('}State')), 'Unknown')
        return jsonify(online=True, state=state, host=PRINTER)
    except (requests.RequestException, ValueError): return jsonify(online=False, state='Unavailable', host=PRINTER)

@app.post('/api/scan')
@auth
def scan():
    data=request.get_json() or {}; size=data.get('size','letter'); mode=data.get('mode','RGB24'); dpi=data.get('dpi',200); intent=data.get('intent','Document')
    if size not in SIZES or mode not in ('RGB24','Grayscale8') or dpi not in (75,100,200,300,600,1200,2400) or intent not in ('Document','Photo'): abort(400, 'Invalid scan settings')
    width,height=SIZES[size]
    if width * height * (dpi / 300) ** 2 > 50_000_000: abort(400, 'This size and resolution are too large. Choose a smaller area or lower resolution.')
    xml=f'''<?xml version="1.0" encoding="UTF-8"?><scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:Version>2.0</pwg:Version><scan:Intent>{intent}</scan:Intent><pwg:ScanRegions><pwg:ScanRegion><pwg:Height>{height}</pwg:Height><pwg:Width>{width}</pwg:Width><pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset></pwg:ScanRegion></pwg:ScanRegions><pwg:InputSource>Platen</pwg:InputSource><scan:ColorMode>{mode}</scan:ColorMode><scan:XResolution>{dpi}</scan:XResolution><scan:YResolution>{dpi}</scan:YResolution><pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat></scan:ScanSettings>'''
    r=requests.post(BASE+'/eSCL/ScanJobs', data=xml, headers={'Content-Type':'text/xml'}, timeout=20)
    if r.status_code in (409, 503): abort(502, 'Scanner is busy or the printer needs attention. Check the paper tray and printer display, then try again.')
    if r.status_code not in (201, 202): abort(502, f'Scanner rejected the job (HTTP {r.status_code}).')
    loc=r.headers.get('Location',''); parsed=urlparse(loc)
    if not parsed.path.startswith('/eSCL/ScanJobs/'): abort(502,'Scanner returned an invalid job address')
    job=BASE+parsed.path.rstrip('/')
    try:
        ident=uuid.uuid4().hex; work=folder()/(ident+'.working'); size_bytes=0
        try:
            with requests.get(job+'/NextDocument',timeout=180,stream=True) as result:
                result.raise_for_status()
                with work.open('wb') as output:
                    for chunk in result.iter_content(chunk_size=65536):
                        size_bytes+=len(chunk)
                        if size_bytes>50*1024*1024: abort(502,'The scanned image is too large. Try a lower resolution.')
                        output.write(chunk)
            with Image.open(work) as image: image.verify()
            (folder()/(ident+'.dpi')).write_text(str(dpi))
            work.replace(folder()/(ident+'.jpg'))
        except Exception:
            work.unlink(missing_ok=True); (folder()/(ident+'.dpi')).unlink(missing_ok=True)
            raise
        return jsonify(id=ident)
    finally:
        try: requests.delete(job,timeout=3)
        except requests.RequestException: pass

@app.route('/api/pages', methods=['GET', 'PUT'])
@auth
def pages():
    available=[p.stem for p in sorted(folder().glob('*.jpg'), key=lambda p: p.stat().st_mtime)]
    order_file=DATA/'page-order.json'
    if request.method=='PUT':
        order=(request.get_json(silent=True) or {}).get('pages')
        if not isinstance(order,list) or len(order)>10000 or any(not isinstance(x,str) for x in order) or len(set(order))!=len(order) or any(x not in available for x in order): abort(400,'Invalid page order.')
        tmp=order_file.with_suffix('.tmp');tmp.write_text(json.dumps(order));tmp.replace(order_file)
    try: saved=json.loads(order_file.read_text())
    except (OSError,ValueError): saved=[]
    if not isinstance(saved,list): saved=[]
    order=[x for x in saved if x in available]
    return jsonify(pages=order+[x for x in available if x not in order], undo={x:undo_count(x) for x in available})

def undo_count(ident):
    return sum((PAGES/(ident+f'.undo{n}')).is_file() for n in range(1,UNDO_DEPTH+1))

def commit_image(ident, image):
    (PAGES/(ident+'.thumb')).unlink(missing_ok=True)
    path=PAGES/(ident+'.jpg'); next_path=PAGES/(ident+'.working')
    try:
        image.save(next_path,format='JPEG',quality=95)
        (PAGES/(ident+f'.undo{UNDO_DEPTH}')).unlink(missing_ok=True)
        for n in range(UNDO_DEPTH-1,0,-1):
            previous=PAGES/(ident+f'.undo{n}')
            if previous.exists(): previous.replace(PAGES/(ident+f'.undo{n+1}'))
        path.replace(PAGES/(ident+'.undo1'))
        try: next_path.replace(path)
        except OSError:
            (PAGES/(ident+'.undo1')).replace(path)
            raise
    finally: next_path.unlink(missing_ok=True)

def serialized(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        with (DATA/'image-operations.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return f(*args,**kwargs)
    return wrapped

@app.post('/api/page/<ident>/undo')
@auth
@serialized
def undo_page(ident):
    if len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(404)
    path=folder()/(ident+'.jpg'); previous=folder()/(ident+'.undo1')
    if not path.is_file(): abort(404)
    if not previous.is_file(): abort(409,'There are no earlier edits to undo.')
    previous.replace(path)
    (folder()/(ident+'.thumb')).unlink(missing_ok=True)
    for n in range(2,UNDO_DEPTH+1):
        older=folder()/(ident+f'.undo{n}')
        if older.exists(): older.replace(folder()/(ident+f'.undo{n-1}'))
    return jsonify(ok=True,undo=undo_count(ident))

@app.route('/api/page/<ident>', methods=['GET', 'DELETE'])
@auth
def page(ident):
    if len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(404)
    path=folder()/(ident+'.jpg')
    if not path.is_file(): abort(404)
    if request.method == 'DELETE':
        for item in folder().glob(ident+'.*'): item.unlink(missing_ok=True)
        return jsonify(ok=True)
    return send_file(path, mimetype='image/jpeg', max_age=0)

@app.get('/api/page/<ident>/thumbnail')
@auth
@serialized
def thumbnail(ident):
    if len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(404)
    path=folder()/(ident+'.jpg'); thumb=folder()/(ident+'.thumb')
    if not path.is_file(): abort(404)
    if not thumb.is_file():
        with Image.open(path) as image:
            image.thumbnail((1200,1200))
            image.convert('RGB').save(thumb,format='JPEG',quality=80,optimize=True)
    return send_file(thumb,mimetype='image/jpeg',max_age=0)

@app.post('/api/page/<ident>/rotate')
@auth
@serialized
def rotate(ident):
    if len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(404)
    path=folder()/(ident+'.jpg')
    if not path.is_file(): abort(404)
    with Image.open(path) as image:
        rotated=image.transpose(Image.Transpose.ROTATE_270)
    commit_image(ident,rotated)
    return jsonify(ok=True,undo=undo_count(ident))


@app.route('/api/settings', methods=['GET', 'PUT'])
@auth
def preferences():
    if request.method == 'PUT':
        value = (request.get_json(silent=True) or {}).get('retention_days')
        if type(value) is not int or not 1 <= value <= 365: abort(400, 'Retention must be between 1 and 365 days.')
        tmp=SETTINGS.with_suffix('.tmp'); tmp.write_text(json.dumps({'retention_days':value})); tmp.replace(SETTINGS)
        cleanup()
    return jsonify(settings())

@app.get('/api/supplies')
@auth
def supplies():
    try:
        response=requests.get(BASE+'/DevMgmt/ConsumableConfigDyn.xml',timeout=5); response.raise_for_status()
        root=ET.fromstring(response.content); inks=[]
        for item in root.iter():
            if item.tag.split('}')[-1] != 'ConsumableInfo': continue
            fields={x.tag.split('}')[-1]: x.text for x in item.iter() if x.text and x.text.strip()}
            code=fields.get('ConsumableLabelCode')
            if code not in ('K','CMY'): continue
            level=fields.get('ConsumablePercentageLevelRemaining')
            inks.append({'name':'Black' if code=='K' else 'Tri-color', 'level':int(level) if level and level.isdigit() else None, 'state':fields.get('ConsumableState','unknown'), 'cartridge':fields.get('ConsumableSelectibilityNumber')})
        response=requests.get(BASE+'/DevMgmt/ProductStatusDyn.xml',timeout=5); response.raise_for_status()
        root=ET.fromstring(response.content)
        alerts=[x.text for x in root.iter() if x.tag.split('}')[-1]=='ProductStatusAlertID']
        return jsonify(ink=inks, alerts=alerts)
    except (requests.RequestException, ET.ParseError): abort(502,'Could not read printer supplies.')

@app.post('/api/page/<ident>/duplicate')
@auth
def duplicate(ident):
    if len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(404)
    path=folder()/(ident+'.jpg')
    if not path.is_file(): abort(404)
    new=uuid.uuid4().hex
    shutil.copyfile(path, folder()/(new+'.jpg')); shutil.copyfile(folder()/(ident+'.dpi'),folder()/(new+'.dpi'))
    return jsonify(id=new)

@app.post('/api/page/<ident>/edit')
@auth
@serialized
def edit_page(ident):
    from PIL import ImageEnhance, ImageOps
    if len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(404)
    path=folder()/(ident+'.jpg')
    if not path.is_file(): abort(404)
    data=request.get_json(silent=True) or {}
    try:
        brightness=float(data.get('brightness',0)); contrast=float(data.get('contrast',0)); angle=int(data.get('rotation',0)); scale=int(data.get('scale',100)); crop=data.get('crop',[0,0,1,1])
        if not (-100<=brightness<=100 and -100<=contrast<=100 and angle in (0,90,180,270) and 10<=scale<=200 and len(crop)==4): raise ValueError()
        x,y,w,h=map(float,crop)
        if not (0<=x<1 and 0<=y<1 and 0<w<=1 and 0<h<=1 and x+w<=1.00001 and y+h<=1.00001): raise ValueError()
    except (ValueError,TypeError): abort(400,'Invalid image adjustments.')
    with Image.open(path) as original:
        image=ImageOps.exif_transpose(original).convert('RGB')
        if angle: image=image.rotate(-angle,expand=True)
        if (x,y,w,h)!=(0,0,1,1): image=image.crop((round(x*image.width),round(y*image.height),round((x+w)*image.width),round((y+h)*image.height)))
        if image.width<2 or image.height<2: abort(400,'Crop area is too small.')
        if image.width * image.height * (scale / 100) ** 2 > 50_000_000: abort(400,'Resized image is too large.')
        if scale != 100: image=image.resize((max(1,round(image.width*scale/100)),max(1,round(image.height*scale/100))),Image.Resampling.LANCZOS)
        if brightness: image=ImageEnhance.Brightness(image).enhance(1+brightness/100)
        if contrast: image=ImageEnhance.Contrast(image).enhance(1+contrast/100)
    commit_image(ident,image)
    return jsonify(ok=True,undo=undo_count(ident))

@app.post('/api/export')
@auth
@serialized
def export():
    data=request.get_json() or {}; ids=data.get('pages',[]); fmt=data.get('format','pdf')
    if not isinstance(ids,list) or not ids or len(ids)>50 or fmt not in ('pdf','jpeg','tiff'): abort(400)
    if fmt=='jpeg' and len(ids)!=1: abort(400,'JPEG supports one page; use PDF or TIFF for multiple pages.')
    paths=[]; resolutions=[]; pixels=0
    for ident in ids:
        if not isinstance(ident,str) or len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(400)
        path=folder()/(ident+'.jpg')
        if not path.is_file(): abort(404)
        with Image.open(path) as image:
            pixels += image.width * image.height
            if pixels > 80_000_000: abort(400,'These pages are too large to export together. Use a lower resolution or export fewer pages.')
        paths.append(path); resolutions.append(int((folder()/(ident+'.dpi')).read_text()))
    if fmt=='tiff' and len(set(resolutions))>1: abort(400,'Use the same resolution for every page in a TIFF, or export as PDF.')
    ext={'pdf':'pdf','jpeg':'jpg','tiff':'tiff'}[fmt]; ident=uuid.uuid4().hex
    filename='scan-'+time.strftime('%Y-%m-%d-%H%M%S')+'.'+ext; dest=DOCUMENTS/(ident+'.'+ext)
    try:
        if fmt=='pdf':
            from pypdf import PdfWriter
            writer=PdfWriter()
            for path,dpi in zip(paths,resolutions):
                with Image.open(path) as original:
                    image=original.convert('RGB'); page=io.BytesIO(); image.save(page,format='PDF',resolution=dpi); image.close(); page.seek(0); writer.append(page)
            with dest.open('wb') as output: writer.write(output)
            writer.close()
        elif fmt=='jpeg':
            shutil.copyfile(paths[0],dest)
        else:
            images=[]
            try:
                for path in paths:
                    with Image.open(path) as image: images.append(image.convert('RGB'))
                images[0].save(dest,format='TIFF',save_all=True,append_images=images[1:],compression='tiff_deflate',dpi=(resolutions[0],resolutions[0]))
            finally:
                for image in images: image.close()
        (DOCUMENTS/(ident+'.json')).write_text(json.dumps({'name':filename,'ext':ext,'created':time.time(),'pages':len(ids)}))
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return send_file(dest,mimetype={'pdf':'application/pdf','jpeg':'image/jpeg','tiff':'image/tiff'}[fmt],as_attachment=True,download_name=filename)


@app.get('/api/documents')
@auth
def documents():
    items=[]
    for path in DOCUMENTS.glob('*.json'):
        try: items.append({'id':path.stem,**json.loads(path.read_text())})
        except (OSError,ValueError): continue
    return jsonify(documents=sorted(items,key=lambda x:x['created'],reverse=True))

@app.route('/api/documents/<ident>', methods=['GET','DELETE'])
@auth
def document(ident):
    if len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(404)
    meta=DOCUMENTS/(ident+'.json')
    if not meta.is_file(): abort(404)
    item=json.loads(meta.read_text()); path=DOCUMENTS/(ident+'.'+item['ext'])
    if request.method=='DELETE':
        path.unlink(missing_ok=True); meta.unlink(missing_ok=True); return jsonify(ok=True)
    if not path.is_file(): abort(404)
    return send_file(path,as_attachment=True,download_name=item['name'])

# Print previews are estimates of page layout, not the printer's color profile.
def print_options(data, total):
    import re
    size=data.get('size','Letter'); color=data.get('color','color'); quality=data.get('quality','Normal')
    orientation=data.get('orientation','portrait'); ranges=data.get('pages','').strip(); fit=data.get('fit','fit'); margins=data.get('margins','normal')
    try: copies=int(data.get('copies',1)); nup=int(data.get('nup',1)); scale=int(data.get('scale',100))
    except (ValueError,TypeError): abort(400,'Invalid print settings.')
    if size not in ('Letter','A4') or color not in ('color','monochrome') or quality not in ('FastDraft','Normal','Best') or orientation not in ('portrait','landscape') or fit not in ('fit','scale') or margins not in ('normal','narrow') or not 1<=copies<=20 or nup not in (1,2,4) or not 25<=scale<=200: abort(400,'Invalid print settings.')
    selected=[]
    if ranges:
        if not re.fullmatch(r'[0-9, -]{1,100}', ranges): abort(400,'Use page ranges such as 1-3, 5.')
        for part in ranges.split(','):
            pair=part.strip().split('-')
            if len(pair)>2 or any(not x.strip().isdigit() for x in pair): abort(400,'Use page ranges such as 1-3, 5.')
            lo=int(pair[0]); hi=int(pair[-1])
            if lo<1 or hi<lo or hi>total: abort(400,'Page range is outside this document.')
            selected.extend(range(lo,hi+1))
        selected=sorted(set(selected))
    else: selected=list(range(1,total+1))
    if not selected: abort(400,'Choose at least one page.')
    return dict(size=size,color=color,quality=quality,orientation=orientation,pages=ranges,copies=copies,nup=nup,scale=scale,fit=fit,margins=margins,selected=selected)

def text_pages(path):
    import textwrap
    lines=[]
    for line in path.read_text(encoding='utf-8').splitlines(): lines.extend(textwrap.wrap(line,90) or [''])
    return [lines[i:i+60] for i in range(0,len(lines),60)] or [[]]

def print_document(ident):
    if len(ident)!=32 or not all(c in '0123456789abcdef' for c in ident): abort(404)
    meta=PRINTS/(ident+'.json')
    if not meta.is_file(): abort(404)
    data=json.loads(meta.read_text()); path=PRINTS/(ident+data['ext'])
    if not path.is_file(): abort(404)
    return path,data

@app.post('/api/print/upload')
@auth
def print_upload():
    upload=request.files.get('file')
    if not upload: abort(400,'Choose a file.')
    ext=Path(upload.filename or '').suffix.lower()
    if ext not in ('.pdf','.jpg','.jpeg','.png','.txt'): abort(400,'Choose a PDF, JPEG, PNG, or text file.')
    ident=uuid.uuid4().hex; path=PRINTS/(ident+ext); upload.save(path)
    try:
        if ext == '.pdf':
            info=subprocess.run(['pdfinfo',str(path)],capture_output=True,text=True,timeout=20)
            if info.returncode: abort(400,'This PDF could not be opened. It may be damaged or password protected.')
            line=next((line for line in info.stdout.splitlines() if line.startswith('Pages:')),None)
            total=int(line.split(':',1)[1].strip()) if line else 0
        elif ext in ('.jpg','.jpeg','.png'):
            try:
                with Image.open(path) as image:
                    if image.width*image.height > 50_000_000: abort(400,'This image is too large to print safely.')
                    image.verify()
            except (OSError, SyntaxError, ValueError): abort(400,'This image could not be opened. It may be damaged.')
            total=1
        else:
            if path.stat().st_size > 2*1024*1024: abort(400,'Text files must be 2 MB or smaller.')
            try: total=len(text_pages(path))
            except UnicodeDecodeError: abort(400,'Text files must use UTF-8 encoding.')
        if not 1<=total<=1000: abort(400,'This file has too many pages.')
        meta={'name':Path(upload.filename).name[:200], 'ext':ext, 'pages':total, 'created':time.time(), 'jobs':[]}
        (PRINTS/(ident+'.json')).write_text(json.dumps(meta))
        return jsonify(id=ident,name=meta['name'],pages=total)
    except Exception:
        path.unlink(missing_ok=True)
        raise

def preview_page(path, meta, number):
    if meta['ext']=='.pdf':
        result=subprocess.run(['pdftoppm','-f',str(number),'-l',str(number),'-singlefile','-scale-to','1200','-png',str(path)],capture_output=True,timeout=30)
        if result.returncode: abort(400,'Could not render this PDF page.')
        with Image.open(io.BytesIO(result.stdout)) as im: return im.convert('RGB')
    if meta['ext']=='.txt':
        from PIL import ImageDraw
        image=Image.new('RGB',(850,1100),'white'); draw=ImageDraw.Draw(image)
        draw.multiline_text((45,45),'\n'.join(text_pages(path)[number-1]),fill='black',spacing=4)
        return image
    with Image.open(path) as im: return im.convert('RGB')

@app.get('/api/print/preview/<ident>')
@auth
@serialized
def print_preview(ident):
    from PIL import ImageOps
    path,meta=print_document(ident); opts=print_options(request.args,meta['pages'])
    try: sheet=int(request.args.get('sheet',1))
    except ValueError: abort(400)
    sheets=(len(opts['selected'])+opts['nup']-1)//opts['nup']
    if not 1<=sheet<=sheets: abort(400,'Invalid preview sheet.')
    width,height=(850,1100) if opts['size']=='Letter' else (827,1169)
    if opts['orientation']=='landscape': width,height=height,width
    canvas=Image.new('RGB',(width,height),'white'); margin=35 if opts['margins']=='normal' else 15; cols=1 if opts['nup']==1 else 2; rows=2 if opts['nup']==4 else 1
    cellw=(width-2*margin)//cols; cellh=(height-2*margin)//rows
    subset=opts['selected'][(sheet-1)*opts['nup']:sheet*opts['nup']]
    for index,page_num in enumerate(subset):
        image=preview_page(path,meta,page_num)
        if opts['color']=='monochrome': image=ImageOps.grayscale(image).convert('RGB')
        factor=min(cellw/image.width,cellh/image.height)
        if opts['fit']=='scale': factor *= opts['scale']/100
        image=image.resize((max(1,int(image.width*factor)),max(1,int(image.height*factor))))
        # Clip to the selected page cell.
        cell=Image.new('RGB',(cellw,cellh),'white'); cell.paste(image,((cellw-image.width)//2,(cellh-image.height)//2))
        canvas.paste(cell,(margin+(index%cols)*cellw,margin+(index//cols)*cellh))
    out=io.BytesIO(); canvas.save(out,'PNG'); out.seek(0)
    return send_file(out,mimetype='image/png',max_age=0)

@app.post('/api/print/submit/<ident>')
@auth
def print_submit(ident):
    import re
    path,meta=print_document(ident); opts=print_options(request.get_json(silent=True) or {},meta['pages'])
    args=['lp','-d',QUEUE,'-n',str(opts['copies']),'-t',meta['name'],'-o',f'ColorModel={"RGB" if opts["color"] == "color" else "KGray"}','-o',f'PageSize={opts["size"]}','-o',f'OutputMode={opts["quality"]}','-o',f'number-up={opts["nup"]}','-o',f'orientation-requested={4 if opts["orientation"] == "landscape" else 3}']
    if opts['pages']: args += ['-o',f'page-ranges={opts["pages"]}']
    margin='18' if opts['margins']=='normal' else '9'
    for edge in ('top','bottom','left','right'): args += ['-o',f'page-{edge}={margin}']
    args += ['-o','fit-to-page' if opts['fit']=='fit' else f'scaling={opts["scale"]}',str(path)]
    result=subprocess.run(args,capture_output=True,text=True,timeout=30)
    if result.returncode: abort(502,'Print submission failed: '+result.stderr.strip()[:250])
    match=re.search(r'\b'+re.escape(QUEUE)+r'-([0-9]+)\b',result.stdout)
    job=match.group(0) if match else None
    meta['jobs'].append({'id':job,'time':time.time(),'options':opts}); (PRINTS/(ident+'.json')).write_text(json.dumps(meta))
    return jsonify(job=job,message='Queued for printing')

@app.get('/api/print/jobs')
@auth
def print_jobs():
    import re
    result=subprocess.run(['lpstat','-W','all','-l','-o',QUEUE],capture_output=True,text=True,timeout=10)
    states={}; current=None
    for line in result.stdout.splitlines():
        match=re.match(r'^('+re.escape(QUEUE)+r'-[0-9]+)\s+',line)
        if match: current=match.group(1); states[current]={'summary':line.strip(), 'details':[]}
        elif current and line.strip(): states[current]['details'].append(line.strip())
    jobs=[]
    for p in PRINTS.glob('*.json'):
        try:
            meta=json.loads(p.read_text())
            for job in meta.get('jobs',[]): jobs.append({'document':p.stem,'name':meta['name'],**job,'cups':states.pop(job.get('id'),None)})
        except (ValueError,OSError,KeyError): continue
    jobs.sort(key=lambda x:x['time'],reverse=True)
    other=[{'id':k,'cups':v} for k,v in states.items()]
    ipp_states={}
    for test in ('get-jobs.test','get-completed-jobs.test'):
        info=subprocess.run(['ipptool','-tv','ipp://localhost/printers/'+QUEUE,test],capture_output=True,text=True,timeout=8)
        current=None
        for line in info.stdout.splitlines():
            match=re.search(r'job-id \(integer\) = ([0-9]+)',line)
            if match:
                current=QUEUE+'-'+match.group(1);ipp_states[current]={}
            elif current:
                state=re.search(r'job-state \(enum\) = ([^\n]+)',line)
                reason=re.search(r'job-state-reasons \(.*?\) = ([^\n]+)',line)
                if state: ipp_states[current]['state']=state.group(1).strip()
                if reason: ipp_states[current]['reason']=reason.group(1).strip()
    for job in jobs+other:
        state=ipp_states.get(job.get('id'),{})
        job['state']=state.get('state','unknown');job['reason']=state.get('reason','')
    printer=subprocess.run(['lpstat','-p',QUEUE],capture_output=True,text=True,timeout=5).stdout.strip()
    return jsonify(jobs=jobs,other=other,printer=printer)

def cups_action(action,job=None):
    request_data=json.dumps({'action':action,'job':job}).encode()+b'\n'
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(15);client.connect('/data/cups/state/control.sock');client.sendall(request_data)
            response=client.recv(4096)
        result=json.loads(response)
    except (OSError,ValueError): abort(502,'The print control service is unavailable.')
    if not result.get('ok'): abort(502,result.get('error') or 'Could not change the print job.')
    return result

@app.post('/api/print/cancel/<job>')
@auth
def print_cancel(job):
    import re
    if not re.fullmatch(re.escape(QUEUE)+r'-[0-9]+',job): abort(400)
    cups_action('cancel',job)
    return jsonify(ok=True)

@app.post('/api/print/queue/<action>')
@auth
def print_queue_action(action):
    if action not in ('pause','resume'): abort(400)
    cups_action(action)
    return jsonify(ok=True)

@app.post('/api/print/retry/<job>')
@auth
def print_retry(job):
    import re
    if not re.fullmatch(re.escape(QUEUE)+r'-[0-9]+',job): abort(400)
    cups_action('retry',job)
    return jsonify(ok=True)


def device_jobs():
    response=requests.get(BASE+'/Jobs/JobList',timeout=8); response.raise_for_status()
    root=ET.fromstring(response.content); jobs=[]
    for item in root:
        if item.tag.split('}')[-1]!='Job': continue
        fields={child.tag.split('}')[-1]:child.text for child in item if child.text and child.text.strip()}
        uri=fields.get('JobUrl',''); state=fields.get('JobState','')
        if fields.get('JobCategory')=='Print' and state not in ('Completed','Canceled','Aborted') and uri.startswith('/Jobs/JobList/') and uri.rsplit('/',1)[-1].isdigit():
            jobs.append({'id':uri.rsplit('/',1)[-1],'state':state,'source':fields.get('JobSource','unknown'),'alert':fields.get('ProductStatusAlertID')})
    return jobs

@app.get('/api/printer/jobs')
@auth
def printer_jobs():
    try: return jsonify(jobs=device_jobs())
    except (requests.RequestException, ET.ParseError): abort(502,'Could not read jobs stored in the printer.')

@app.post('/api/printer/jobs/<ident>/cancel')
@auth
def cancel_printer_job(ident):
    if not ident.isascii() or not ident.isdigit() or len(ident)>10: abort(400,'Invalid printer job.')
    try:
        if not any(job['id']==ident for job in device_jobs()): abort(404,'This printer job is no longer active.')
        uri='/Jobs/JobList/'+ident
        xml=f'<?xml version="1.0" encoding="UTF-8"?><Job xmlns="http://www.hp.com/schemas/imaging/con/ledm/jobs/2009/04/30"><JobUrl>{uri}</JobUrl><JobState>Canceled</JobState></Job>'
        response=requests.put(BASE+uri,data=xml,headers={'Content-Type':'text/xml'},timeout=12); response.raise_for_status()
        remaining=any(job['id']==ident for job in device_jobs())
        return jsonify(confirmed=not remaining, message='Cancellation requested.' if remaining else 'Printer job canceled.')
    except (requests.RequestException, ET.ParseError): abort(502,'The printer did not confirm cancellation. Check its job list or display.')

@app.post('/api/print/continue')
@auth
def continue_printing():
    try:
        response=requests.get(BASE+'/DevMgmt/ProductStatusDyn.xml',timeout=5);response.raise_for_status()
        root=ET.fromstring(response.content)
        alerts=[x.text for x in root.iter() if x.tag.split('}')[-1]=='ProductStatusAlertID']
    except (requests.RequestException, ET.ParseError): abort(502,'Could not check the printer. Confirm it is online and try again.')
    if 'trayEmptyOrOpen' in alerts:
        return jsonify(continued=False,message='The printer still reports that the tray is empty or open. Load paper, close the tray, and check again. If its display asks for confirmation, press OK on the printer.')
    cups_action('resume')
    return jsonify(continued=True,message='The tray alert is clear and the print queue has resumed.')
