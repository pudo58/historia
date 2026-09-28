import { test, expect, type Page } from '@playwright/test';
import { demoVideo } from './demo-video';

async function fixture(page:Page, status='running') {
  const mutations:string[]=[];
  const errors:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));
  const config={width:1280,height:720,frames:81,fps:16,steps:4,shot_seconds:5.0625};
  const scenes=Array.from({length:14},(_,i)=>({id:`s${i}`,scene_id:`s${i}`,position:i+1,revision:1,title:`Cảnh ${i+1} · Kinh thành Thăng Long`,chapter:'Chương 1',narration:'Lời dẫn minh họa',visual_prompt:'Mô tả cảnh',citations:[],character_ids:[],reference_ids:[],camera:'Chậm',seed:42,review_note:'',audio_seconds:23.3,shot_count:5,completed_shots:i<5?5:0,status:i===5?'running':i<5?'completed':'queued',configs:[config],job_id:`j${i}`,shots:Array.from({length:5},(_,n)=>({index:n,stage:`clip-${n}`,job_id:`j${i}`,artifact_id:i<5?`a${i}-${n}`:undefined,state:i<5?'downloaded':'pending',config,timing:{total_seconds:123}}))}));
  const project={id:'p',title:'Thăng Long · Một ngày trong kinh thành',topic:'Dữ liệu minh họa local · không render GPU',style:'Điện ảnh',quality:'final',duration_seconds:300,duration_minutes:5,era:'Thời Lý',location:'Thăng Long',voice:'default',pronunciation:'',sources:[],characters:[],outline:[],outline_approved:true,scenes,updated_at:'2026-09-28T00:00:00Z'};
  const jobs=scenes.map((s,i)=>({id:`j${i}`,scene_id:s.id,project_id:'p',kind:'clip',host_id:'h',status:s.status,progress:i<5?100:0,created_at:'2026-09-28T00:00:00Z',snapshot:{production_run_id:'r',scene:s,project},result:{}}));
  const run={id:'r',created_at:'2026-09-28T00:00:00Z',status,stage:'clip',job_ids:jobs.map(j=>j.id),measured_audio_seconds:326.2,target_duration_seconds:300,duration_review_required:false,snapshot:{scenes},checkpoint:{jobs:Object.fromEntries(jobs.map(j=>['clip:'+j.scene_id,j.id])),current_job_id:'j5',media:{},artifact_ids:status==='completed'?['film','srt']:[]}};
  const entries=Array.from({length:1205},(_,i)=>({id:i+1,job_id:i%2?'j1':'j0',scene_id:i%2?'s1':'s0',scene_title:i%2?'Cảnh 2':'Cảnh 1',kind:'clip',created_at:'2026-09-28T12:00:00Z',message:`Sự kiện ${i+1} · Đã lưu và kiểm tra output`,level:i%10===0?'error':'info',stage:'clip-0',legacy:false,source:'studio'}));
  let failLogs=false;
  await page.route('**/api/**',async route=>{
    const request=route.request(),url=new URL(request.url()),path=url.pathname;
    if(request.method()!=='GET'){mutations.push(path);if(path.endsWith('/pause'))run.status='paused';return route.fulfill({json:{}});}
    if(path.includes('/events')) {
      if(failLogs)return route.fulfill({status:503,json:{detail:'Disconnected'}});
      let rows=entries.filter(e=>(!url.searchParams.get('job_id')||e.job_id===url.searchParams.get('job_id'))&&(!url.searchParams.get('q')||e.message.includes(url.searchParams.get('q')!))&&(!url.searchParams.get('level')||url.searchParams.get('level')!.split(',').includes(e.level)));
      if(path.endsWith('/export'))return route.fulfill({body:rows.map(e=>JSON.stringify(e)).join('\n'),headers:{'content-type':'application/x-ndjson','content-disposition':'attachment; filename="journal.ndjson"'}});
      const after=Number(url.searchParams.get('after')),before=Number(url.searchParams.get('before'));
      if(after)rows=rows.filter(e=>e.id>after);if(before)rows=rows.filter(e=>e.id<before);
      return route.fulfill({json:{items:after?rows.slice(0,200):rows.slice(-200),has_more:rows.length>200}});
    }
    if(path==='/api/studio/projects')return route.fulfill({json:[project]});
    if(path==='/api/studio/projects/p')return route.fulfill({json:project});
    if(path==='/api/studio/jobs')return route.fulfill({json:jobs});
    if(path==='/api/hosts')return route.fulfill({json:[{id:'h',label:'A100 · Demo',state:'ready'}]});
    if(path.endsWith('/production-runs'))return route.fulfill({json:[run]});
    if(path.endsWith('/performance'))return route.fulfill({json:{scenes,completed_shots:25,remaining_shots:45,unmeasured_scenes:0,eta_seconds:null,estimated_remaining_usd:null,measured_audio_seconds:326.2}});
    if(path.includes('/artifacts/')&&path.endsWith('/file')) {
      const video=Buffer.from(demoVideo,'base64');
      const range=request.headers()['range'];
      const offset=range?Number(range.match(/bytes=(\d+)/)?.[1]||0):0;
      return route.fulfill({status:range?206:200,contentType:'video/mp4',body:video.subarray(offset),headers:{'accept-ranges':'bytes','content-length':String(video.length-offset),...(range?{'content-range':`bytes ${offset}-${video.length-1}/${video.length}`}:{})}});
    }
    if(path.endsWith('/artifacts'))return route.fulfill({json:[]});
    return route.fulfill({json:[]});
  });
  return {mutations,errors,entries,disconnect:()=>{failLogs=true;}};
}

test('Video has one player, stable selection and contextual journal',async({page})=>{
  const state=await fixture(page);
  await page.goto('/?page=projects&project=p&tab=video');
  await expect(page.getByRole('region',{name:'Không gian Video'})).toBeVisible();
  await expect(page.locator('video')).toHaveCount(1);
  await page.getByRole('button',{name:/Cảnh 2 · Kinh thành/}).click();
  await page.getByRole('button',{name:/SHOT 02/}).click();
  const source=await page.locator('video').getAttribute('src');
  await expect.poll(()=>page.locator('video').evaluate((v:HTMLVideoElement)=>v.readyState)).toBeGreaterThan(0);
  await page.locator('video').evaluate((v:HTMLVideoElement)=>{v.currentTime=1.5;});
  await page.waitForTimeout(4500);
  await expect(page.locator('video')).toHaveAttribute('src',source!);
  await expect.poll(()=>page.locator('video').evaluate((v:HTMLVideoElement)=>v.currentTime)).toBeCloseTo(1.5,1);
  await page.getByRole('button',{name:'Xem log',exact:true}).click();
  await expect(page).toHaveURL(/tab=logs/);
  await expect(page).toHaveURL(/log_job=j1/);
  await expect(page.locator('.journal-viewport')).toBeVisible();
  await page.getByRole('button',{name:'← Video'}).click();
  await expect(page.locator('video')).toHaveAttribute('src',source!);
  await expect.poll(()=>page.locator('video').evaluate((v:HTMLVideoElement)=>v.currentTime)).toBeCloseTo(1.5,1);
  expect(await page.locator('video').evaluate((v:HTMLVideoElement)=>v.paused)).toBeTruthy();
  await page.reload();
  await expect(page.locator('video')).toHaveAttribute('src',source!);
  expect(state.mutations).toEqual([]);expect(state.errors).toEqual([]);
});

test('journal server search, older pages, no forced scroll and full export',async({page})=>{
  const state=await fixture(page);
  await page.goto('/?page=projects&project=p&tab=logs');
  await expect(page.locator('.journal-row').first()).toBeVisible();
  await page.getByRole('button',{name:'Tải sự kiện trước đó'}).click();
  await expect(page.getByText('400 dòng đã tải', {exact:false})).toBeVisible();
  await page.locator('.journal-viewport').evaluate(el=>{el.scrollTop=0;el.dispatchEvent(new Event('scroll'));});
  await expect(page.getByRole('checkbox',{name:'Theo dõi dòng mới'})).not.toBeChecked();
  state.entries.push({...state.entries[0],id:1206,message:'New event'});
  await expect(page.getByRole('button',{name:/dòng mới ↓/})).toBeVisible({timeout:8000});
  expect(await page.locator('.journal-viewport').evaluate(el=>el.scrollTop)).toBe(0);
  await page.getByPlaceholder('Tìm trong toàn bộ lịch sử…').fill('Sự kiện 1111');
  await page.getByRole('button',{name:'Tìm',exact:true}).click();
  await expect(page.getByRole('button',{name:/Sự kiện 1111/})).toBeVisible();
  await expect(page.getByText('1 dòng đã tải', {exact:false})).toBeVisible();
  const downloaded=page.waitForEvent('download');await page.getByRole('link',{name:'Tải nhật ký'}).click();await downloaded;
  state.disconnect();await page.getByRole('button',{name:'Cập nhật',exact:true}).click();
  await expect(page.getByText(/Mất kết nối nhật ký/)).toBeVisible({timeout:10000});
  expect(state.mutations).toEqual([]);expect(state.errors).toEqual([]);
});

test('pause is explicit and sent only once; task table uses names',async({page})=>{
  const state=await fixture(page);
  await page.goto('/?page=projects&project=p&tab=video');
  await page.getByRole('button',{name:'Tạm dừng',exact:true}).click();
  await expect(page.getByRole('button',{name:'Đối chiếu & tiếp tục'})).toBeVisible();
  expect(state.mutations).toEqual(['/api/studio/production-runs/r/pause']);
  await page.goto('/?page=jobs');
  await expect(page.locator('.task-table tbody tr')).toHaveCount(14);
  await page.getByRole('button',{name:/Cảnh 2 · Kinh thành/}).click();
  await expect(page.getByRole('region',{name:'Chi tiết tác vụ'})).toBeVisible();
  await page.getByRole('button',{name:'Nhật ký ↗',exact:true}).click();
  await expect(page).toHaveURL(/log_job=j1/);
  expect(state.mutations).toHaveLength(1);expect(state.errors).toEqual([]);
});

for(const width of [1920,1366,1024,768])test(`layout ${width}px`,async({page})=>{
  const state=await fixture(page);
  await page.setViewportSize({width,height:768});
  await page.goto('/?page=projects&project=p&tab=video');
  await expect(page.locator('.scene-navigator')).toBeVisible();
  const screen=await page.locator('.film-screen').boundingBox();
  const column=await page.locator('.player-column').boundingBox();
  expect(Math.abs(screen!.width-column!.width)).toBeLessThan(2);
  if(width>=1366) {
    expect(screen!.y).toBeLessThan(600);
    expect((await page.locator('.scene-scroll').boundingBox())!.y).toBeLessThan(700);
  }
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
  await page.screenshot({path:`test-results/video-${width}.png`,fullPage:true});
  await page.getByRole('tab',{name:'Nhật ký',exact:true}).click();
  await expect(page.locator('.journal-row').first()).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
  await page.screenshot({path:`test-results/log-${width}.png`,fullPage:true});
  expect(state.errors).toEqual([]);
});

for(const status of ['pause_requested','paused','reconciling','failed','completed'])test(`production state ${status} is read only until action`,async({page})=>{
  const state=await fixture(page,status);
  await page.goto('/?page=projects&project=p&tab=video');
  await expect(page.locator('.video-workspace')).toBeVisible();
  if(['paused','failed','reconciling'].includes(status))await expect(page.getByRole('button',{name:'Đối chiếu & tiếp tục'})).toBeVisible();
  else await expect(page.getByRole('button',{name:'Đối chiếu & tiếp tục'})).toHaveCount(0);
  if(status==='completed')await expect(page.locator('video')).toHaveAttribute('src','/api/studio/artifacts/film/file');
  await page.getByRole('tab',{name:'Nhật ký',exact:true}).click();
  await expect(page.locator('.journal-viewport')).toBeVisible();
  expect(state.mutations).toEqual([]);expect(state.errors).toEqual([]);
});
