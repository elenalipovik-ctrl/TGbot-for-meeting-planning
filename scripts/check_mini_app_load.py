"""Local diagnostic load only: temporary SQLite, synthetic sessions/calendar, loopback.

No arbitrary target URL, .env loading, external network or real notifications.
Use --seconds 900 for the proposed sustained profile; this is not VPS/WebView QA.
"""
import argparse
import ctypes
import gc
from contextlib import closing
import http.client
import json
import math
import os
import platform
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from meeting_bot.api.app import create_app
from meeting_bot.api.config import ApiConfig
from meeting_bot.api.telegram_auth import TelegramIdentity
from meeting_bot.database import connect
from meeting_bot.service import BookingService
from meeting_bot.technical_log import configure_logging
from meeting_bot.visitor_flow import VisitorFlow
from meeting_bot.worker import run_due_jobs

ADMIN=900000; BASE=910000


class Calendar:
    def freebusy(self,*args):return []


def rss_bytes():
    if sys.platform=="win32":
        class Memory(ctypes.Structure):
            _fields_=[("cb",ctypes.c_ulong),("PageFaultCount",ctypes.c_ulong)]+[(name,ctypes.c_size_t) for name in ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage","QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage")]
        memory=Memory();memory.cb=ctypes.sizeof(memory)
        get_process=ctypes.windll.kernel32.GetCurrentProcess
        get_process.restype=ctypes.c_void_p
        get_memory=ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes=[ctypes.c_void_p,ctypes.POINTER(Memory),ctypes.c_ulong]
        get_memory.restype=ctypes.c_int
        handle=get_process()
        if not get_memory(handle,ctypes.byref(memory),memory.cb):return None
        return memory.WorkingSetSize
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):return int(line.split()[1])*1024
    except OSError:pass
    return None


def percentile(values,fraction):
    if not values:return None
    return round(sorted(values)[max(0,math.ceil(len(values)*fraction)-1)],2)


def measure(seconds,clients):
    with tempfile.TemporaryDirectory(prefix="mini-app-ma5-") as folder:
        path=Path(folder)/"test.sqlite";log=Path(folder)/"api.jsonl"
        service=BookingService(path,administrator_id=ADMIN,logger=configure_logging(log))
        service.initialize();now=int(time.time())
        template=service.create_template(ADMIN,"Synthetic load meeting","","",now)
        zone=ZoneInfo("Europe/Moscow")
        day=datetime.fromtimestamp(now,zone).date()+timedelta(days=1)
        while day.weekday() in (2,5,6):day+=timedelta(days=1)
        start=int(datetime.combine(day,datetime.min.time(),zone).timestamp())+16*3600
        interval=service.publish_interval(ADMIN,start,start+7200,"Europe/Moscow",now)
        for uid in [BASE+i for i in range(clients)]+[BASE+clients]:
            service.request_access(uid,"synthetic",now);service.decide_access(ADMIN,uid,"allowed",now)
            service.record_consent(uid,"load-v1",now)
        calendar=Calendar()
        app=create_app(service,calendar,ApiConfig("000000:synthetic-load-only-token","load-v1","https://example.invalid/privacy",()))
        # Auth signatures/rate limits have separate tests. Seed 20 independent sessions
        # before measurement; all measured business requests use the real HTTP boundary.
        tokens=[app.state.store.exchange(TelegramIdentity(BASE+i,"synthetic",now,"load-"+str(i)),now)["bearer"] for i in range(clients)]
        sock=socket.socket();sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,access_log=False,log_config=None,log_level="critical",proxy_headers=False,server_header=False,timeout_graceful_shutdown=5))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        for _ in range(100):
            if server.started:break
            if not thread.is_alive():raise RuntimeError("Local test server did not start")
            time.sleep(.05)
        if not server.started:raise RuntimeError("Local test server startup timeout")
        stop=threading.Event();gate=threading.Event();reads=[];writes=[];statuses=Counter();errors=Counter()
        worker_times=[];chat_times=[];background_errors=[];rss=[];disk=[];log_sizes=[]
        def background():
            flow=VisitorFlow(service,calendar,policy_url="https://example.invalid/privacy",policy_version="load-v1")
            gate.wait()
            while not stop.is_set():
                try:
                    at=int(time.time());tick=time.perf_counter()
                    run_due_jobs(path,at,calendar=calendar,administrator_id=ADMIN)
                    worker_times.append((time.perf_counter()-tick)*1000)
                    tick=time.perf_counter();flow.handle(BASE+clients,"synthetic","menu",at)
                    chat_times.append((time.perf_counter()-tick)*1000)
                    value=rss_bytes()
                    if value is not None:rss.append(value)
                    disk.append(sum(p.stat().st_size for p in Path(folder).iterdir() if p.is_file()))
                    log_sizes.append(log.stat().st_size if log.exists() else 0)
                except Exception as error:background_errors.append(type(error).__name__)
                stop.wait(1)
        bg=threading.Thread(target=background,daemon=True);bg.start()
        def client(index):
            connection=http.client.HTTPConnection("127.0.0.1",port,timeout=8)
            headers={"Authorization":"Bearer "+tokens[index]}
            gate.wait()
            body=json.dumps(dict(revision=0,template_id=template,interval_id=interval,starts_at=start,duration_minutes=30,video_service="telemost"))
            try:
                tick=time.perf_counter()
                connection.request("POST","/api/v1/holds",body,headers={**headers,"Content-Type":"application/json","Idempotency-Key":"load-contention-key-"+str(index)})
                response=connection.getresponse();payload=json.loads(response.read())
                writes.append((time.perf_counter()-tick)*1000);statuses["write_"+str(response.status)]+=1
                if response.status not in (200,409):errors[payload.get("error",{}).get("code","invalid_response")]+=1
                routes=["/bootstrap","/templates","/me/bookings?group=review","/me/profile"]
                count=0
                while not stop.is_set():
                    tick=time.perf_counter()
                    connection.request("GET","/api/v1"+routes[count%len(routes)],headers=headers)
                    response=connection.getresponse();payload=json.loads(response.read())
                    reads.append((time.perf_counter()-tick)*1000);statuses["read_"+str(response.status)]+=1
                    if response.status!=200:errors[payload.get("error",{}).get("code","invalid_response")]+=1
                    count+=1
                    stop.wait(2)  # 30 reads/minute/session: production limits unchanged.
            except Exception as error:errors[type(error).__name__]+=1
            finally:connection.close()
        started=time.perf_counter();cpu=time.process_time()
        try:
            with ThreadPoolExecutor(max_workers=clients) as pool:
                futures=[pool.submit(client,i) for i in range(clients)]
                gate.set();stop.wait(seconds);stop.set()
                for future in futures:future.result(timeout=12)
            bg.join(12)
            elapsed=time.perf_counter()-started;cpu_used=time.process_time()-cpu
            with closing(connect(path)) as c:
                conflicts=c.execute("SELECT COUNT(*) FROM bookings a JOIN bookings b ON a.id<b.id WHERE a.status IN ('held','pending','awaiting_link','creating_calendar','confirmed','attention') AND b.status IN ('held','pending','awaiting_link','creating_calendar','confirmed','attention') AND a.starts_at<b.ends_at+1800 AND b.starts_at<a.ends_at+1800").fetchone()[0]
                bookings=c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
                integrity=c.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_keys=c.execute("PRAGMA foreign_key_check").fetchall()
            return dict(kind="local_diagnostic_not_release_acceptance",platform=platform.system(),python=platform.python_version(),clients=clients,requested_seconds=seconds,elapsed_seconds=round(elapsed,2),network="loopback_no_throttling",auth="synthetic_sessions_seeded_before_measurement",calendar="stub_no_external_calls",read_requests=len(reads),read_p50_ms=percentile(reads,.5),read_p95_ms=percentile(reads,.95),read_max_ms=round(max(reads,default=0),2),contention_write_p95_ms=percentile(writes,.95),statuses=dict(statuses),errors=dict(errors),bookings=bookings,overlap_pairs=conflicts,integrity=integrity,foreign_key_violations=len(foreign_keys),worker_ticks=len(worker_times),worker_p95_ms=percentile(worker_times,.95),chat_ticks=len(chat_times),chat_p95_ms=percentile(chat_times,.95),background_errors=background_errors,cpu_seconds=round(cpu_used,2),cpu_core_equivalent=round(cpu_used/elapsed,3),process_rss_peak_bytes=max(rss,default=None),temporary_storage_peak_bytes=max(disk,default=0),technical_log_peak_bytes=max(log_sizes,default=0),passed_local_checks=not errors and not background_errors and conflicts==0 and bookings==1 and statuses["write_200"]==1 and statuses["write_409"]==clients-1 and integrity=="ok" and not foreign_keys and bool(reads) and percentile(reads,.95)<=1000)
        finally:
            stop.set();bg.join(12);server.should_exit=True;thread.join(12);sock.close()
            if thread.is_alive():raise RuntimeError("Local test server did not stop")
            for handler in list(service.logger.handlers):handler.close();service.logger.removeHandler(handler)
            gc.collect()  # sqlite context managers commit/rollback; finalise released handles on Windows.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds",type=int,default=60)
    parser.add_argument("--clients",type=int,default=20)
    parser.add_argument("--report",type=Path)
    args=parser.parse_args()
    if not 10<=args.seconds<=900 or not 2<=args.clients<=20:parser.error("seconds 10..900, clients 2..20")
    result=measure(args.seconds,args.clients)
    output=json.dumps(result,ensure_ascii=False,indent=2)
    if args.report:args.report.write_text(output+"\n",encoding="utf8")
    print(output)
    return 0 if result["passed_local_checks"] else 1


if __name__=="__main__":raise SystemExit(main())
