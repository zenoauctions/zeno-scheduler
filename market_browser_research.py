#!/usr/bin/env python3
"""Bounded public-browser fallback. No lot data or credentials are persisted."""
import html,json,os,re,shutil,subprocess,urllib.parse,urllib.request

URL=os.environ["ZENO_MARKET_RESEARCH_URL"]
HEAD={"Accept":"application/json","Authorization":"Bearer "+os.environ["GH_ACTIONS_TOKEN"],"OAI-Sites-Authorization":"Bearer "+os.environ["SITES_DISPATCH_TOKEN"],"X-Zeno-Scheduler":"github-actions"}
SLUG={"audemars piguet":"audemarspiguet","jaeger lecoultre":"jaegerlecoultre","patek philippe":"patekphilippe","tag heuer":"tagheuer","vacheron constantin":"vacheronconstantin"}

def api(method,payload=None):
 body=None if payload is None else json.dumps(payload,separators=(",",":")).encode()
 request=urllib.request.Request(URL,data=body,method=method,headers={**HEAD,**({"Content-Type":"application/json"} if body else {})})
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

def main():
 chrome=next((path for name in ("google-chrome","google-chrome-stable","chromium") if (path:=shutil.which(name))),None)
 if not chrome:raise SystemExit("Browser executable unavailable")
 tasks=api("GET").get("tasks",[]);accepted=0
 for task in tasks:accepted+=int(api("POST",{"lot_key":task["lot_key"],"observations":research(chrome,task)}).get("accepted",0))
 print(f"Browser market research: {len(tasks)} task(s), {accepted} verified observation(s).")

if __name__=="__main__":main()
