// Uses discovered installed Playwright and a fresh browser profile; never attaches to user's browser.
const fs=require('fs'),path=require('path'),{pathToFileURL}=require('url');
const input=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
let pw;try{pw=require('playwright')}catch(e){if(!input.playwrightPackage)throw e;pw=require(input.playwrightPackage)}
const {chromium}=pw;
const norm=s=>s.replace(/\s+/g,'');
const assert=(v,msg)=>{if(!v)throw new Error(msg)};
(async()=>{
 const browser=await chromium.launch({executablePath:input.browser,headless:true,args:['--disable-background-networking']});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:1,isMobile:true,hasTouch:true});
  const page=await context.newPage();const requests=[],errors=[];
  await context.route('**/*',route=>{const u=route.request().url();if(/^https?:/.test(u)){requests.push(u);route.abort();}else route.continue();});
  page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url())});
  await page.goto(pathToFileURL(input.html).href);await page.waitForSelector('article');
  const widths=async()=>page.evaluate(()=>({innerWidth,scrollWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth}));
  const mobile=await widths();assert(mobile.innerWidth===390&&mobile.scrollWidth===390&&mobile.bodyWidth===390,'390px overflow '+JSON.stringify(mobile));
  assert(input.articles.length>0,'empty canonical articles cannot validate filters');
  const cards=page.locator('article');assert(await cards.count()===input.articles.length,'article count mismatch');
  const bodies=[];
  for(let i=0;i<input.articles.length;i++){
   const card=cards.nth(i),a=input.articles[i],n=a.cites[0][0],u=a.cites[0][1];
   await card.scrollIntoViewIfNeeded();assert(await card.isVisible(),'initial article hidden '+n);
   assert(await card.getAttribute('data-ref')===n&&await card.getAttribute('data-url')===u,'article identity '+n);
   assert(await card.getAttribute('data-region')===a.region,'candidate region mismatch '+n);
   assert(await card.getAttribute('data-direction')===a.direction,'candidate topic mismatch '+n);
   assert(norm(await card.locator('[data-role=title]').innerText())===norm(a.title),'title '+n);
   assert(norm(await card.locator('[data-role=summary]').innerText())===a.summary,'full summary '+n);
   assert(await card.locator('[data-role=citation]').getAttribute('href')===u,'canonical URL '+n);
   const styles=await card.locator('[data-role=summary]').evaluate(el=>{
    const nodes=[];for(let e=el;e;e=e.parentElement){const s=getComputedStyle(e),r=e.getBoundingClientRect();nodes.push({display:s.display,visibility:s.visibility,opacity:Number(s.opacity),color:s.color,w:r.width,h:r.height,left:r.left,right:r.right});}
    const s=getComputedStyle(el);return {fontSize:parseFloat(s.fontSize),nodes};});
   assert(styles.fontSize>=18,'font too small '+n);
   assert(styles.nodes.every(s=>s.display!=='none'&&s.visibility==='visible'&&s.opacity>0&&s.w>0&&s.h>0&&s.right>0&&s.left<390&&s.color!=='rgba(0, 0, 0, 0)'),'invisible ancestor '+n);
   const detail=card.locator('details');await detail.locator('summary').click();assert(await detail.getAttribute('open')!==null,'details did not open');assert((await widths()).scrollWidth===390,'expanded detail overflow');await detail.locator('summary').click();
   bodies.push({number:n,url:u,summaryLength:a.summary.length,fontSize:styles.fontSize,initialVisible:true,evidenceExpanded:true});
  }
  assert(await page.locator('#count').innerText()===String(input.articles.length),'initial stats');
  const topics=await page.locator('#topic option').evaluateAll(opts=>opts.map(o=>o.value));
  const regions=await page.locator('#region option').evaluateAll(opts=>opts.map(o=>o.value));
  const filters=[];
  for(const t of topics)for(const r of regions){await page.selectOption('#topic',t);await page.selectOption('#region',r);
   const state=await cards.evaluateAll((els,args)=>els.map((e,i)=>({shown:!e.hidden,expected:(!args.t||args.articles[i].direction===args.t)&&(!args.r||args.articles[i].region===args.r)})),{t,r,articles:input.articles});
   assert(state.every(s=>s.shown===s.expected),'filter mismatch');const count=state.filter(s=>s.shown).length;
   assert(await page.locator('#count').innerText()===String(count),'filtered stats mismatch');filters.push({topic:t,region:r,count});}
  for(const r of input.requiredRegions){const f=filters.find(f=>f.topic===''&&f.region===r);assert(f&&f.count>0,'required region filter empty '+r);}
  await page.selectOption('#topic','');await page.selectOption('#region','');
  const first=page.locator('nav [data-target]').first();const target=await first.getAttribute('data-target');await first.click();assert(await page.evaluate(()=>location.hash)==='#'+target,'directory anchor');
  await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(input.evidence,'mobile-390.png'),fullPage:true});
  await cards.first().locator('details summary').click();await page.screenshot({path:path.join(input.evidence,'mobile-390-expanded.png'),fullPage:true});await cards.first().locator('details summary').click();
  const desktopPage=await browser.newPage({viewport:{width:1280,height:900}});
  await desktopPage.goto(pathToFileURL(input.html).href);
  const desktop=await desktopPage.evaluate(()=>({innerWidth,scrollWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth}));
  assert(desktop.innerWidth===1280&&desktop.scrollWidth===1280,'desktop overflow');
  await desktopPage.screenshot({path:path.join(input.evidence,'desktop-1280.png'),fullPage:true});
  await desktopPage.close();
  assert(!requests.length,'external network requests');assert(!errors.length,'browser errors');
  const testedLinks=[];
  await context.unroute('**/*');
  await context.route('**/*',route=>{
   const u=route.request().url();
   if(/^https?:/.test(u)){
    route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><title>Mock</title><body>Offline Destination</body>'});
   }else route.continue();
  });
  const tldrLinks=page.locator('#tldr a[href^="http"]');
  if(await tldrLinks.count()>0){
   const link=tldrLinks.first();const expectedUrl=await link.getAttribute('href');
   const [popup]=await Promise.all([context.waitForEvent('page',{timeout:8000}),link.click()]);
   await popup.waitForLoadState('domcontentloaded');
   assert(popup.url()===expectedUrl,'TLDR link click destination mismatch: '+popup.url()+' vs '+expectedUrl);
   testedLinks.push({section:'tldr',text:await link.innerText(),url:popup.url()});
   await popup.close();
  }
  const horizonLinks=page.locator('#horizon a[href^="http"]');
  if(await horizonLinks.count()>0){
   const link=horizonLinks.first();const expectedUrl=await link.getAttribute('href');
   const [popup]=await Promise.all([context.waitForEvent('page',{timeout:8000}),link.click()]);
   await popup.waitForLoadState('domcontentloaded');
   assert(popup.url()===expectedUrl,'Horizon link click destination mismatch: '+popup.url()+' vs '+expectedUrl);
   testedLinks.push({section:'horizon',text:await link.innerText(),url:popup.url()});
   await popup.close();
  }
  const cardCitation=cards.first().locator('[data-role=citation]');
  const expectedCardUrl=await cardCitation.getAttribute('href');
  const [cardPopup]=await Promise.all([context.waitForEvent('page',{timeout:8000}),cardCitation.click()]);
  await cardPopup.waitForLoadState('domcontentloaded');
  assert(cardPopup.url()===expectedCardUrl,'Card citation click destination mismatch: '+cardPopup.url()+' vs '+expectedCardUrl);
  testedLinks.push({section:'article',text:await cardCitation.innerText(),url:cardPopup.url()});
  await cardPopup.close();
  const pageHtml=await page.content();
  assert(!/\[\[\d+\]\]\(https?:/.test(pageHtml),'raw [[n]](url) syntax remains unparsed');
  assert(!/\[[^\]]+\]\(https?:/.test(pageHtml),'raw [title](url) syntax remains unparsed');
  const totalPre=await page.locator('pre').count();const detailsPre=await page.locator('details pre').count();
  assert(totalPre===detailsPre,'found <pre> outside details evidence ('+totalPre+' vs '+detailsPre+')');
  fs.writeFileSync(path.join(input.evidence,'browser-result.json'),JSON.stringify({ok:true,mode:'REAL_BROWSER_OFFLINE',browser:input.browser,mobile,desktop,bodies,filters,directory:true,networkRequests:requests,pageErrors:errors,testedLinks},null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e.stack);process.exitCode=1});

