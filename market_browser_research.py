#!/usr/bin/env python3
"""Bounded public-browser fallback. No lot data or credentials are persisted."""
import html,json,os,re,shutil,subprocess,urllib.parse,urllib.request
import base64,hashlib,select,sys,tempfile,time,datetime

URL=os.environ["ZENO_MARKET_RESEARCH_URL"]
HEAD={"Accept":"application/json","Authorization":"Bearer "+os.environ["GH_ACTIONS_TOKEN"],"OAI-Sites-Authorization":"Bearer "+os.environ["SITES_DISPATCH_TOKEN"],"X-Zeno-Scheduler":"github-actions"}
SLUG={"audemars piguet":"audemarspiguet","jaeger lecoultre":"jaegerlecoultre","patek philippe":"patekphilippe","tag heuer":"tagheuer","vacheron constantin":"vacheronconstantin"}

def api(method,payload=None,experimental=False):
 body=None if payload is None else json.dumps(payload,separators=(",",":")).encode()
 request=urllib.request.Request(URL+("?mode=a2" if experimental else ""),data=body,method=method,headers={**HEAD,**({"Content-Type":"application/json"} if body else {})})
 with urllib.request.urlopen(request,timeout=120) as response:return json.load(response)

def page(chrome,url):
 result=subprocess.run([chrome,"--headless=new","--disable-gpu","--no-sandbox","--disable-dev-shm-usage","--disable-background-networking","--virtual-time-budget=8000","--dump-dom",url],capture_output=True,text=True,timeout=35)
 output=result.stdout[:5*1024*1024]
 return "" if result.returncode or re.search(r"just a moment|verify you are human|access denied|cf-chl-",output,re.I) else output

def walk(value):
 if isinstance(value,list):
  for child in value:yield from walk(child)
 elif isinstance(value,dict):
  yield value
  for child in value.values():yield from walk(child)

def offers(source,provider,base):
 found={}
 for block in re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',source,re.I):
  try:root=json.loads(html.unescape(block))
  except (ValueError,TypeError):continue
  for product in walk(root):
   types=product.get("@type",[])
   if "Product" not in ([types] if isinstance(types,str) else types):continue
   values=product.get("offers",[]);values=values if isinstance(values,list) else [values]
   for offer in values:
    if not isinstance(offer,dict) or re.search(r"OutOfStock|SoldOut|Discontinued",str(offer.get("availability","")),re.I):continue
    try:url=urllib.parse.urljoin(base,str(offer.get("url") or product.get("url") or ""));price=round(float(offer["price"])*100)
    except (ValueError,TypeError,KeyError):continue
    parsed=urllib.parse.urlparse(url)
    valid=provider=="chrono24" and parsed.hostname=="www.chrono24.it" and re.search(r"-id\d+\.htm$",parsed.path,re.I) or provider=="chronext" and parsed.hostname in ("www.chronext.it","www.chronext.at") and re.search(r"/V[A-Z0-9]+$",parsed.path,re.I)
    if valid and str(offer.get("priceCurrency","")).upper()=="EUR" and 1000<=price<=100_000_000:found[url]={"provider":provider,"source_url":url,"title":str(product.get("name",""))[:240],"reference":str(product.get("mpn",""))[:40],"price_cents":price}
 return list(found.values())

def urls(task):
 brand=task["brand"].strip().lower();slug=SLUG.get(brand,re.sub(r"[^a-z0-9]","",brand));ref=re.sub(r"[^a-z0-9]","",task["reference"].lower())
 result=[("chrono24",f"https://www.chrono24.it/{slug}/ref-{ref}.htm")]
 if brand=="rolex":
  title=task["title"].lower().replace("-"," ");families=[(r"\bdatejust\b","datejust"),(r"\bdaytona\b","cosmograph-daytona"),(r"\bsubmariner\b","submariner"),(r"\bgmt\s*master\b","gmt-master"),(r"\byacht\s*master\b","yacht-master"),(r"\bexplorer\s*(?:ii|2)\b","explorer-ii"),(r"\boyster\s*perpetual\b","oyster-perpetual")]
  family=next((name for pattern,name in families if re.search(pattern,title)),None)
  if family:result.append(("chronext",f"https://www.chronext.it/rolex/{family}/{urllib.parse.quote(task['reference'].strip())}"))
 return result

def research(chrome,task):
 found=[]
 for provider,url in urls(task):
  source=page(chrome,url);found+=offers(source,provider,url)
  if provider=="chrono24" and not found:
   links=[]
   for href in re.findall(r'href=["\']([^"\']+)["\']',source,re.I):
    item=urllib.parse.urljoin(url,html.unescape(href));parsed=urllib.parse.urlparse(item)
    if parsed.hostname=="www.chrono24.it" and re.search(r"-id\d+\.htm$",parsed.path,re.I) and item not in links:links.append(item)
   for item in links[:int(task.get("max_pages",4))]:found+=offers(page(chrome,item),provider,item)
 return found[:24]

def public_url(value,hosts):
 parsed=urllib.parse.urlparse(value)
 return parsed.scheme=="https" and parsed.hostname in hosts and not parsed.port and not parsed.username and not parsed.password

def snapshot(chrome,task):
 """Original rendered DOM plus actual main-document HTTP/redirect receipt.
 Uses the already installed Chrome over a local pipe, with a fresh public-only
 profile. It never parses prices or invokes the legacy observations writer.
 """
 start=time.monotonic();deadline=start+min(35,int(task.get("timeout_ms",35000))/1000)
 result={"receipt_id":task["receipt_id"],"requested_url":task["url"],"outcome":"BLOCKED"}
 hosts=task["allowed_hosts"]
 if not public_url(task["url"],hosts):return result
 process=None;fds=[];profile_dir=tempfile.TemporaryDirectory(prefix="zeno-public-")
 try:
  profile=profile_dir.name
  child_in,writer=os.pipe();reader,child_out=os.pipe();fds=[writer,reader,child_in,child_out]
  def pipes():
   os.dup2(child_in,3);os.dup2(child_out,4)
  process=subprocess.Popen([chrome,"--headless=new","--disable-gpu","--no-sandbox","--disable-dev-shm-usage","--disable-background-networking","--no-first-run","--remote-debugging-pipe","--user-data-dir="+profile],
   pass_fds=tuple(sorted(set([child_in,child_out,3,4]))),preexec_fn=pipes,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  os.close(child_in);os.close(child_out);fds=[writer,reader]
  serial=0;buffer=b"";answers={};documents=[];redirects=[];loaded=None;session=None;frame=None;requests=0
  def send(method,params=None,with_session=True):
   nonlocal serial
   serial+=1;message={"id":serial,"method":method,"params":params or {}}
   if with_session and session:message["sessionId"]=session
   raw=json.dumps(message,separators=(",",":")).encode()+b"\0"
   while raw:raw=raw[os.write(writer,raw):]
   return serial
  def pump():
   nonlocal buffer,loaded,requests
   if time.monotonic()>=deadline:raise TimeoutError("Public browser deadline")
   if not select.select([reader],[],[],min(.2,max(.001,deadline-time.monotonic())))[0]:return
   data=os.read(reader,65536)
   if not data:raise OSError("Browser pipe closed")
   buffer+=data
   if len(buffer)>int(task["max_bytes"])*8+2_000_000:raise ValueError("Browser protocol size limit")
   while b"\0" in buffer:
    raw,buffer=buffer.split(b"\0",1)
    if not raw:continue
    event=json.loads(raw);params=event.get("params",{});method=event.get("method","")
    if "id" in event:answers[event["id"]]=event
    elif method=="Fetch.requestPaused":
     url=params.get("request",{}).get("url","")
     send("Fetch.continueRequest" if public_url(url,hosts) else "Fetch.failRequest",{"requestId":params["requestId"],**({} if public_url(url,hosts) else {"errorReason":"BlockedByClient"})})
    elif method=="Network.responseReceived" and params.get("type")=="Document":documents.append(params)
    elif method=="Network.requestWillBeSent":
     requests+=1
     if params.get("type")=="Document" and params.get("redirectResponse"):redirects.append(params["redirectResponse"]["url"])
    elif method=="Page.loadEventFired":loaded=time.monotonic()
  def call(method,params=None,with_session=True):
   ident=send(method,params,with_session)
   while ident not in answers:pump()
   answer=answers.pop(ident)
   if "error" in answer:raise ValueError("Browser command rejected")
   return answer.get("result",{})
  target=call("Target.createTarget",{"url":"about:blank"},False)["targetId"]
  session=call("Target.attachToTarget",{"targetId":target,"flatten":True},False)["sessionId"]
  call("Page.enable");call("Network.enable");call("Fetch.enable",{"patterns":[{"urlPattern":"*","requestStage":"Request"}]})
  navigation=call("Page.navigate",{"url":task["url"]});frame=navigation.get("frameId")
  if navigation.get("errorText"):raise ValueError("Public navigation rejected")
  while loaded is None or time.monotonic()-loaded<1.5:pump()
  content=call("Runtime.evaluate",{"expression":"({url:location.href,html:document.documentElement.outerHTML})","returnByValue":True})["result"]["value"]
  responses=[p["response"] for p in documents if p.get("frameId")==frame]
  if not responses or responses[-1].get("status")!=200:raise ValueError("Public document HTTP failure")
  final=content["url"];source=content["html"];body=source.encode("utf-8")
  if not public_url(final,hosts) or any(not public_url(u,hosts) for u in redirects):raise ValueError("Public redirect rejected")
  if not body or len(body)>int(task["max_bytes"]) or re.search(r"just a moment|verify you are human|access denied|cf-chl-",source,re.I):raise ValueError("No usable public snapshot")
  result.update(outcome="ACQUIRED",final_url=final,http_status=200,redirects=redirects,bytes_base64=base64.b64encode(body).decode(),
   content_hash=hashlib.sha256(body).hexdigest(),captured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),request_count=requests)
 except (OSError,ValueError,KeyError,TimeoutError,json.JSONDecodeError):pass
 finally:
  if process:
   process.kill()
   try:process.wait(timeout=2)
   except subprocess.TimeoutExpired:pass
  for fd in fds:
   try:os.close(fd)
   except OSError:pass
  profile_dir.cleanup()
 result["elapsed_ms"]=round((time.monotonic()-start)*1000)
 return result

def main():
 chrome=next((path for name in ("google-chrome","google-chrome-stable","chromium") if (path:=shutil.which(name))),None)
 if not chrome:raise SystemExit("Browser executable unavailable")
 if "--a2-only" not in sys.argv:
  tasks=api("GET").get("tasks",[]);accepted=0
  for task in tasks:accepted+=int(api("POST",{"lot_key":task["lot_key"],"observations":research(chrome,task)}).get("accepted",0))
  print(f"Browser market research: {len(tasks)} task(s), {accepted} verified observation(s).")
 tasks=api("GET",experimental=True).get("tasks",[]);accepted=0
 for task in tasks:
  if "receipt_id" not in task:continue
  accepted+=int(api("POST",snapshot(chrome,task),experimental=True).get("accepted",0))
 print(f"Experimental public retrieval: {len(tasks)} task(s), {accepted} receipt(s).")

if __name__=="__main__":main()
