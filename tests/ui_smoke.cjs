const {chromium}=require('playwright');
const fs=require('fs');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:1});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const state={ok:true,is_admin:false,has_access:true,subscribed:true,expires_at:new Date(Date.now()+86400000*4).toISOString(),accounts:[{id:1,phone:'+10000000001',status:'active'},{id:2,phone:'+10000000002',status:'active'}],account_count:2,bot_name:'SendFlow',price_stars:50,sub_days:30,free_days:4,trial_max:5,max_accounts:5,trial_used:true,ai_available:false,owner_contact:'@support',owner_contact_url:'https://t.me/support',ref_hours:6,ref_count:2,ref_link:'https://t.me/SendFlowTgBot?start=11111',templates:[],jobs:[]};
 let confirmed=0,savedSettings=0,scheduleCreated=0;
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(url.hostname==='sf.local'){
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(require('path').join(__dirname,'..','webapp','index.html'),'utf8')});
   if(url.pathname.startsWith('/api/')){
    const b=route.request().postDataJSON()||{};let data={...state};
    if(url.pathname==='/api/auth/verify')data.access_token='dummy-token';
    if(url.pathname==='/api/folders')data={ok:true,folders:[{id:10002,title:'Клиенты · 0001',account_id:1}]};
    if(url.pathname==='/api/action'&&b.action==='preview')data={ok:true,preview_id:'p1',recipients:[{account_id:1,chat_id:-100123,title:'Тестовая группа'}]};
    if(url.pathname==='/api/action'&&b.action==='start'){confirmed++;data={...state};}
    if(url.pathname==='/api/action'&&b.action==='schedule'){scheduleCreated++;data={ok:true,schedule_id:1};}
    if(url.pathname==='/api/settings'){
     if(b.save)savedSettings++;
     data={ok:true,settings:{reply_notify:true,dm_notify:false,autorespond:false,reply_text:'',timezone:'Europe/Berlin',quiet_start:9,quiet_end:18,outside_hours:false}};
    }
    if(url.pathname==='/api/schedules')data={ok:true,schedules:[]};
    return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   }
   return route.fulfill({status:404,body:''});
  }
  if(url.hostname==='telegram.org')return route.fulfill({contentType:'application/javascript',body:"window.Telegram={WebApp:{initData:'dummy',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},HapticFeedback:{impactOccurred(){},notificationOccurred(){}}}};"});
  return route.abort();
 });
 await page.goto('https://sf.local/');await page.waitForFunction(()=>typeof STATE!=='undefined'&&STATE&&STATE.ok);
 await page.evaluate(()=>{finishOnboarding();go('broadcast')});
 await page.waitForSelector('.sf-account');await page.fill('#bc-text','Продам вещь, пишите в личку');
 await page.click('#bc-go');await page.waitForSelector('#sf-confirm');
 if(!(await page.locator('#sf-dialog-body').innerText()).includes('Тестовая группа'))throw Error('Recipients missing');
 await page.screenshot({path:'ui-preview-mobile.png',fullPage:true});
 await page.click('#sf-confirm');await page.waitForTimeout(800);
 if(confirmed!==1)throw Error('Start not called');
 await page.evaluate(()=>go('accounts'));await page.getByRole('button',{name:'Настройки',exact:true}).first().click();
 await page.check('#sf-set-dm_notify');await page.check('#sf-set-autorespond');await page.fill('#sf-set-text','Здравствуйте, скоро отвечу!');
 await page.getByRole('button',{name:'Сохранить настройки',exact:true}).click();await page.waitForTimeout(100);
 if(savedSettings!==1)throw Error('Settings not saved');
 await page.evaluate(()=>go('broadcast'));await page.selectOption('#sf-when','later');await page.fill('#sf-at','2026-11-20T18:30');await page.selectOption('#sf-repeat','weekly');await page.check('.sf-day[value="4"]');
 await page.click('#bc-go');await page.waitForSelector('#sf-confirm');await page.click('#sf-confirm');await page.waitForTimeout(800);
 if(scheduleCreated!==1)throw Error('Schedule not called');
 await page.evaluate(()=>go('sub'));await page.screenshot({path:'ui-pro-mobile.png',fullPage:true});
 if(!(await page.locator('#sub-price').innerText()).includes('30 дней'))throw Error('Price duration missing');
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
 if(overflow)throw Error('Horizontal overflow');
 await page.setViewportSize({width:1280,height:900});await page.evaluate(()=>go('broadcast'));await page.screenshot({path:'ui-desktop.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 console.log('UI PASS: mobile and desktop; recipient preview, start, account settings, schedule, PRO; no JavaScript errors or horizontal overflow.');
 await browser.close();process.exit(0);
})().catch(e=>{console.error(e);process.exit(1)});
