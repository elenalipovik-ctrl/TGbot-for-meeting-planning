import type {BookingSummary} from './generated/api';
export const statuses:Record<string,string>={held:'Время удерживается',pending:'Ожидает решения',awaiting_link:'Добавляется ссылка',creating_calendar:'Создаётся встреча',confirmed:'Встреча подтверждена',attention:'Требует внимания',rejected:'Заявка отклонена',expired:'Срок истёк',cancelled_by_visitor:'Отменена вами',cancelled_by_admin:'Отменена администратором'};
export const platform=(value:string|null|undefined)=>value==='zoom'?'Zoom':'Яндекс Телемост';
export const date=(ts:number)=>new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'long',weekday:'short'}).format(ts*1000);
export const time=(ts:number)=>new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'}).format(ts*1000);
export const dayKey=(ts:number)=>new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(ts*1000);
export const emailValid=(value:string)=>value.trim().length<=500&&/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
export const phoneValid=(value:string)=>!value.trim()||/^[+0-9() .-]{7,25}$/.test(value.trim());
export function groupOf(item:BookingSummary,now:number){if(['pending','awaiting_link','creating_calendar','attention'].includes(item.status))return 'review';if(item.status==='confirmed'&&item.ends_at>now)return 'upcoming';return 'history';}
export function remaining(expires:number,now:number){const seconds=Math.max(0,expires-now);return Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0');}
