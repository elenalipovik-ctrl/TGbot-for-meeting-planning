import type {ErrorResponse, SessionData} from './generated/api';
export type Result<T>={data:T;server_time:number;request_id:string;next_cursor:string|null};
export class ApiError extends Error {
 code:string;retryable:boolean;requestId:string;fields:Record<string,string>;
 constructor(code:string,message:string,retryable=false,requestId='',fields:Record<string,string>={}){super(message.replace(/[—–]/g,'-'));this.code=code;this.retryable=retryable;this.requestId=requestId;this.fields=fields;}
}
let bearer:string|null=null;
let offset=0;
const keys=new Map<string,string>();
export const serverNow=()=>Math.floor((Date.now()+offset)/1000);
export function setBearer(value:string|null){bearer=value;keys.clear();}
export async function api<T>(path:string,method='GET',body?:unknown,command=true):Promise<Result<T>>{
 const signature=method+path+JSON.stringify(body);
 const headers:Record<string,string>={};if(bearer)headers.Authorization='Bearer '+bearer;
 if(body!==undefined)headers['Content-Type']='application/json';
 const mutation=method!=='GET'&&command;
 if(mutation){if(!keys.has(signature))keys.set(signature,crypto.randomUUID());headers['Idempotency-Key']=keys.get(signature)!;}
 const abort=new AbortController();const timeout=setTimeout(()=>abort.abort(),25000);
 try{
 const response=await fetch(path.startsWith('/local-preview/')?path:'/api/v1'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),credentials:'omit',cache:'no-store',signal:abort.signal});
 const payload=await response.json();
 if(Number.isInteger(payload.server_time))offset=payload.server_time*1000-Date.now();
 if(!response.ok){const e=payload as ErrorResponse;if(!e.error.retryable)keys.delete(signature);if(response.status===401)bearer=null;throw new ApiError(e.error.code,e.error.message,e.error.retryable,e.request_id,e.error.field_errors);}
 keys.delete(signature);return payload as Result<T>;
 }catch(e){if(e instanceof ApiError)throw e;throw new ApiError('NETWORK','Не удалось получить ответ. Проверьте соединение и повторите действие. Повтор не создаст вторую заявку.',true);}
 finally{clearTimeout(timeout);}
}
export async function enter(persona:string):Promise<boolean>{
 setBearer(null);
 if(['127.0.0.1','localhost'].includes(location.hostname)){
  const r=await api<SessionData&{preview:boolean}>('/local-preview/session','POST',{persona},false);
  if(!r.data.preview)throw new ApiError('AUTH_INVALID','Локальный просмотр не подтверждён.');
  setBearer(r.data.bearer);return true;
 }
 if(!window.Telegram?.WebApp){await new Promise<void>((resolve,reject)=>{const script=document.createElement('script');script.src='https://telegram.org/js/telegram-web-app.js';script.onload=()=>resolve();script.onerror=()=>reject(new ApiError('NETWORK','Не загрузился Telegram. Переоткройте приложение.'));document.head.append(script);});}
 const tg=window.Telegram?.WebApp;
 if(!tg?.initData)throw new ApiError('AUTH_INVALID','Откройте приложение из личного чата Telegram-бота.');
 tg.ready();tg.expand();
 const r=await api<SessionData>('/session','POST',{init_data:tg.initData},false);setBearer(r.data.bearer);return false;
}
export function clearMemory(){setBearer(null);}
