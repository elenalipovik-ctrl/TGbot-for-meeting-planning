"""Small real-WebView probe for MA-2; the product frontend is MA-3."""
import secrets
from starlette.responses import HTMLResponse

PAGE = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Проверка входа Mini App</title>
<body><main><h1>Проверка входа Telegram</h1><p id="status">Откройте эту страницу через тестового Telegram-бота.</p><button id="login">Проверить вход</button> <button id="logout">Завершить сессию</button></main>
<script src="https://telegram.org/js/telegram-web-app.js"></script><script nonce="__NONCE__">
(()=>{let bearer=null;const status=document.getElementById('status');
const show=(text)=>{status.textContent=text;};
async function call(path,method='GET',body){const headers={};if(bearer)headers.Authorization='Bearer '+bearer;if(body)headers['Content-Type']='application/json';
const r=await fetch('/api/v1'+path,{method,headers,body:body?JSON.stringify(body):undefined,cache:'no-store',credentials:'omit'});const value=await r.json();
if(!r.ok){if(r.status===401)bearer=null;throw new Error(value.error?.code||'REQUEST_FAILED');}return value.data;}
document.getElementById('login').onclick=async()=>{bearer=null;try{const tg=window.Telegram?.WebApp;if(!tg?.initData){show('Нет данных входа. Переоткройте страницу через тестового бота.');return;}
tg.ready();const session=await call('/session','POST',{init_data:tg.initData});bearer=session.bearer;const profile=await call('/bootstrap');
show('Вход проверен. Роль: '+profile.role+'. Допуск: '+profile.access_status+'. Сессия до '+new Date(session.expires_at*1000).toLocaleTimeString()+'.');
}catch(e){show('Вход не выполнен: '+e.message+'. Переоткройте приложение.');}};
document.getElementById('logout').onclick=async()=>{try{if(bearer)await call('/session','DELETE');show('Сессия завершена.');}catch(e){show('Сессия закрыта в приложении.');}finally{bearer=null;}};
window.addEventListener('pagehide',()=>{bearer=null;});})();</script></body></html>'''


def install_smoke_page(app):
    @app.get("/auth-check", include_in_schema=False)
    def auth_check():
        nonce=secrets.token_urlsafe(24)
        return HTMLResponse(PAGE.replace("__NONCE__",nonce),headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'nonce-"+nonce+"' https://telegram.org; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org;",
            "Cache-Control":"no-store", "Referrer-Policy":"no-referrer"})
