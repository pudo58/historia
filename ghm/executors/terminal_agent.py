"""One-shot RPC agent sent in-memory to terminal-only SSH servers.

No listening port, daemon, credentials on argv, or dependency installation.
Requests arrive only after raw-mode READY; shell echo is never parsed as output.
"""

AGENT = r'''
import base64,hashlib,json,os,signal,subprocess,sys,threading,urllib.request,urllib.error
n=sys.argv[1]; child=None; attrs=None
def emit(kind,value):
 print(n+':'+kind+':'+base64.b64encode(value).decode(),flush=True)
def stop(signum,frame):
 if child is not None and child.poll() is None:
  try: os.killpg(child.pid,signal.SIGTERM)
  except ProcessLookupError: pass
  try: child.wait(timeout=4)
  except subprocess.TimeoutExpired:
   try: os.killpg(child.pid,signal.SIGKILL)
   except ProcessLookupError: pass
 raise SystemExit(130)
for sig in (signal.SIGHUP,signal.SIGTERM,signal.SIGINT): signal.signal(sig,stop)
try:
 if os.isatty(0):
  import termios,tty
  attrs=termios.tcgetattr(0); tty.setraw(0)
 print('\n'+n+':READY',flush=True)
 line=sys.stdin.buffer.readline(48*1024*1024)
 if not line.endswith(b'\n'): raise ValueError('Request limit')
 c=json.loads(line); op=c['op']; rc=0
 if op=='run':
  child=subprocess.Popen(['/bin/sh','-c',c['command']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
  def feed():
   try: child.stdin.write(base64.b64decode(c['input'])); child.stdin.close()
   except (BrokenPipeError,OSError): pass
  writer=threading.Thread(target=feed,daemon=True); writer.start()
  while True:
   b=os.read(child.stdout.fileno(),16384)
   if not b: break
   emit('DATA',b)
  rc=child.wait(); writer.join(timeout=1)
 elif op=='http':
  port=int(c['port']); path=c['path']
  if not 1024<=port<=65535 or not path.startswith('/') or path.startswith('//') or '\r' in path or '\n' in path: raise ValueError('HTTP target')
  class NoRedirect(urllib.request.HTTPRedirectHandler):
   def redirect_request(self,*a,**k): return None
  opener=urllib.request.build_opener(NoRedirect,urllib.request.ProxyHandler({}))
  headers={k:v for k,v in c['headers'].items() if k.lower() not in ('host','connection','transfer-encoding','accept-encoding','content-length')}
  req=urllib.request.Request('http://127.0.0.1:'+str(port)+path,data=base64.b64decode(c['body']) or None,headers=headers,method=c['method'])
  try: response=opener.open(req,timeout=c.get('timeout',60))
  except urllib.error.HTTPError as ex: response=ex
  with response:
   emit('HEAD',json.dumps({'status':response.status,'headers':dict(response.headers)}).encode())
   while True:
    b=response.read(49152)
    if not b: break
    emit('DATA',b)
 elif op=='read':
  h=hashlib.sha256(); size=0
  with open(c['path'],'rb') as f:
   while True:
    b=f.read(49152)
    if not b: break
    h.update(b); size+=len(b); emit('DATA',b)
  emit('HASH',json.dumps({'sha256':h.hexdigest(),'size':size}).encode())
 elif op=='write':
  import pathlib
  p=pathlib.Path(c['path']); b=base64.b64decode(c['body'])
  if hashlib.sha256(b).hexdigest()!=c['sha256']: raise ValueError('Checksum')
  temp=p.with_name(p.name+'.upload-'+n)
  with temp.open('xb') as f: f.write(b)
  os.replace(temp,p)
  emit('HASH',json.dumps({'sha256':c['sha256'],'size':len(b)}).encode())
 else: raise ValueError('Unknown operation')
 emit('EXIT',str(rc).encode())
except Exception as ex:
 emit('ERROR',type(ex).__name__.encode())
finally:
 if attrs is not None:
  try: termios.tcsetattr(0,termios.TCSANOW,attrs)
  except Exception: pass
'''
