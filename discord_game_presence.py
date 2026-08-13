#!/usr/bin/env python3
"""Privacy-preserving fallback Discord game presence monitor."""
from __future__ import annotations
import argparse, json, os, re, selectors, socket, struct, subprocess, sys, time, tomllib, uuid
from dataclasses import dataclass
from pathlib import Path

APP="discord-game-presence"; MAX=16*1024*1024; ACTIVE_CHECK_SECONDS=15
OP_HANDSHAKE=0;OP_FRAME=1;OP_CLOSE=2;OP_PING=3;OP_PONG=4
RPC_RESPONSE_TIMEOUT_SECONDS=5;RPC_SAFETY_REFRESH_SECONDS=10*60;RPC_READ_SIZE=64*1024
RECONNECT_DELAYS=(0.5,1,2,4,8,16,32,60)
@dataclass(frozen=True)
class Game:
    name:str; application_id:str; process_names:tuple[str,...]
@dataclass(frozen=True)
class Config:
    poll:float; games:tuple[Game,...]; names:dict[str,tuple[int,...]]; truncated:frozenset[str]

def load_config(path:Path)->Config:
    with path.open("rb") as f: raw=tomllib.load(f)
    poll=float(raw.get("poll_interval_seconds",60))
    if not 1 <= poll <= 300: raise ValueError("poll_interval_seconds must be between 1 and 300")
    games=[]
    for i,g in enumerate(raw.get("games",[]),1):
        aid=str(g.get("application_id","")); names=tuple(str(x).casefold() for x in g.get("process_names",[]))
        if not g.get("name") or not aid.isdigit() or not names: raise ValueError(f"invalid games entry {i}")
        games.append(Game(str(g["name"]),aid,names))
    if not games: raise ValueError("at least one [[games]] entry is required")
    lookup={}
    for i,g in enumerate(games):
        for name in g.process_names:lookup.setdefault(name,[]).append(i)
    names={name:tuple(indices) for name,indices in lookup.items()}
    return Config(poll,tuple(games),names,frozenset(name[:15] for name in names if len(name)>15))

def proc_start_ticks(pid:int,proc:Path=Path("/proc"))->int:
    fields=(proc/str(pid)/"stat").read_text().rsplit(") ",1)[1].split()
    return int(fields[19])
def proc_clock(proc:Path=Path("/proc"))->tuple[int,int]:
    boot=int((proc/"stat").read_text().split("btime ",1)[1].splitlines()[0])
    return boot,os.sysconf("SC_CLK_TCK")
def proc_start_time(ticks:int,clock:tuple[int,int])->int:
    boot,ticks_per_second=clock
    return boot + ticks//ticks_per_second
def process_basenames(comm:str,cmdline:bytes)->set[str]:
    argv=[x.decode(errors="replace") for x in cmdline.split(b"\0") if x]
    return {comm.strip().casefold()}|{x.replace("\\","/").rsplit("/",1)[-1].casefold() for x in argv}
def candidate_indices(config:Config,comm:str,cmdline:bytes|None=None)->set[int]:
    base=comm.strip().casefold(); found=set(config.names.get(base,()))
    # Linux comm is limited to 15 bytes. Read argv only when comm could be a
    # truncated configured basename; this retains Wine/Proton matching without
    # decoding every process command line.
    plausible=base in config.truncated
    if cmdline is not None:
        for name in process_basenames(comm,cmdline):found.update(config.names.get(name,()))
    elif plausible:
        return {-1}|found
    return found
def detect(config:Config,proc:Path=Path("/proc"),clock:tuple[int,int]|None=None):
    found={}
    for p in proc.iterdir():
        if not p.name.isdigit(): continue
        try:
            comm=(p/"comm").read_text()
            # Proton exposes Windows argv entries (for example
            # S:\\steamapps\\...\\RocketLeague.exe), which pathlib on Linux does
            # not recognize as paths. Normalize both separator styles while
            # preserving spaces inside a native executable's argv[0].
            indices=candidate_indices(config,comm)
            if -1 in indices:
                indices=candidate_indices(config,comm,(p/"cmdline").read_bytes())
            for i in indices:
                ticks=proc_start_ticks(int(p.name),proc)
                found.setdefault(i,(int(p.name),ticks))
                if i==0:return config.games[0],int(p.name),ticks,proc_start_time(ticks,clock or proc_clock(proc))
        except (FileNotFoundError,PermissionError,ProcessLookupError,ValueError): pass
    if found:
        i=min(found);pid,ticks=found[i]
        return config.games[i],pid,ticks,proc_start_time(ticks,clock or proc_clock(proc))
    return None

def process_is_same(pid:int,start_ticks:int,proc:Path=Path("/proc"))->bool:
    try:return proc_start_ticks(pid,proc)==start_ticks
    except (FileNotFoundError,PermissionError,ProcessLookupError,ValueError):return False

def frame(op:int,data:dict)->bytes:
    b=json.dumps(data,separators=(",",":")).encode(); return struct.pack("<II",op,len(b))+b
def raw_frame(op:int,body:bytes)->bytes:return struct.pack("<II",op,len(body))+body
def exact(s,n):
    b=bytearray()
    while len(b)<n:
        c=s.recv(n-len(b))
        if not c:raise ConnectionError("Discord closed RPC")
        b+=c
    return bytes(b)
def recv(s):
    op,n=struct.unpack("<II",exact(s,8))
    if n>MAX:raise ValueError("oversized RPC frame")
    return op,json.loads(exact(s,n))

class RpcDisconnected(ConnectionError):pass
class RpcProtocolError(RuntimeError):pass

class Rpc:
    def __init__(self):
        self.s=None;self.aid=None;self.rx=bytearray();self.tx=bytearray()
        self.pending_nonce=None;self.pending_deadline=0.0;self.pending_kind=None
        self.desired_start=None;self.confirmed_start=None;self.next_refresh=0.0
    def close(self):
        if self.s:
            try:self.s.close()
            except OSError:pass
        self.s=None;self.aid=None;self.rx.clear();self.tx.clear();self._clear_pending();self.confirmed_start=None
    def _clear_pending(self):
        self.pending_nonce=None;self.pending_deadline=0.0;self.pending_kind=None
    def connect(self,path:Path,aid:str):
        self.close();s=socket.socket(socket.AF_UNIX);s.settimeout(3)
        try:
            s.connect(str(path));s.sendall(frame(OP_HANDSHAKE,{"v":1,"client_id":aid}))
            while True:
                op,msg=recv(s)
                if op==OP_PING:s.sendall(frame(OP_PONG,msg));continue
                if op==OP_CLOSE:raise RpcDisconnected(f"Discord closed RPC: {msg}")
                if op!=OP_FRAME or not isinstance(msg,dict) or msg.get("evt")!="READY":raise RpcProtocolError(f"unexpected handshake response: {msg}")
                break
            s.setblocking(False);self.s=s;self.aid=aid
        except Exception:
            s.close();raise
    def selector_events(self):return selectors.EVENT_READ|(selectors.EVENT_WRITE if self.tx else 0)
    def queue_activity(self,start:int|None,kind:str,now:float)->bool:
        if not self.s or self.pending_nonce is not None:return False
        nonce=str(uuid.uuid4());activity=None if start is None else {"timestamps":{"start":start},"instance":True}
        self.tx.extend(frame(OP_FRAME,{"cmd":"SET_ACTIVITY","args":{"pid":os.getpid(),"activity":activity},"nonce":nonce}))
        self.pending_nonce=nonce;self.pending_deadline=now+RPC_RESPONSE_TIMEOUT_SECONDS;self.pending_kind=kind
        return True
    def write_ready(self):
        while self.tx:
            try:sent=self.s.send(self.tx)
            except BlockingIOError:return
            except OSError as e:raise RpcDisconnected(str(e)) from e
            if sent==0:raise RpcDisconnected("Discord RPC write returned zero")
            del self.tx[:sent]
    def read_ready(self,now:float):
        while True:
            try:chunk=self.s.recv(RPC_READ_SIZE)
            except BlockingIOError:break
            except OSError as e:raise RpcDisconnected(str(e)) from e
            if not chunk:raise RpcDisconnected("Discord closed RPC")
            self.rx.extend(chunk)
        results=[]
        while len(self.rx)>=8:
            op,n=struct.unpack("<II",self.rx[:8])
            if n>MAX:raise RpcProtocolError("oversized RPC frame")
            if len(self.rx)<8+n:break
            body=bytes(self.rx[8:8+n]);del self.rx[:8+n]
            if op==OP_PING:self.tx.extend(raw_frame(OP_PONG,body))
            elif op==OP_PONG:continue
            elif op==OP_CLOSE:
                try:reason=json.loads(body)
                except json.JSONDecodeError:reason=body.decode(errors="replace")
                raise RpcDisconnected(f"Discord closed RPC: {reason}")
            elif op==OP_FRAME:
                try:msg=json.loads(body)
                except json.JSONDecodeError as e:raise RpcProtocolError("invalid RPC JSON") from e
                if not isinstance(msg,dict):raise RpcProtocolError("RPC payload is not an object")
                result=self._handle_message(msg,now)
                if result:results.append(result)
            else:raise RpcProtocolError(f"unexpected RPC opcode {op}")
        return results
    def _handle_message(self,msg:dict,now:float):
        if self.pending_nonce is None or msg.get("nonce")!=self.pending_nonce:return None
        kind=self.pending_kind;self._clear_pending()
        if msg.get("evt")=="ERROR":raise RpcProtocolError(f"Discord rejected activity: {msg}")
        if kind in ("publish","refresh"):
            self.confirmed_start=self.desired_start;self.next_refresh=now+RPC_SAFETY_REFRESH_SECONDS
            return "published" if kind=="publish" else "refreshed"
        if kind=="clear":self.confirmed_start=None;return "cleared"
        raise RpcProtocolError("response matched an unknown request")

class LogArbiter:
    connection_pattern=re.compile(r"Socket (Opened|Close): (\d+) (\d+) \(active: \d+\)")
    message_pattern=re.compile(r"Socket Message: (\d+) ")
    def __init__(self,path:Path):self.path=path;self.pos=0;self.external=0;self.valid=False;self.sockets={}
    def consume(self,line:str):
        if m:=self.connection_pattern.search(line):
            action,sid,aid=m.groups()
            if action=="Opened":self.sockets[sid]=[aid,False]
            else:self.sockets.pop(sid,None)
        elif m:=self.message_pattern.search(line):
            if m.group(1) in self.sockets:self.sockets[m.group(1)][1]=True
    def initialize(self):
        if not self.path.is_file():raise FileNotFoundError(self.path)
        self.sockets={}
        for line in self.path.read_text(errors="replace").splitlines()[-2000:]:self.consume(line)
        self.pos=self.path.stat().st_size;self.valid=True
    def update(self,own_aid:str|None):
        st=self.path.stat()
        if st.st_size<self.pos:self.initialize()
        else:
            with self.path.open(errors="replace") as f:
                f.seek(self.pos)
                for line in f:self.consume(line)
                self.pos=f.tell()
        self.external=sum(1 for aid,sent_message in self.sockets.values() if sent_message and aid!=own_aid)

def notify(message):
    try:subprocess.run(["notify-send","--app-name",APP,"Discord game presence paused",message],timeout=5,check=False)
    except (OSError,subprocess.TimeoutExpired):pass
def log(msg):print(msg,flush=True)

def run(config_path:Path,once=False):
    runtime=Path(os.getenv("XDG_RUNTIME_DIR",f"/run/user/{os.getuid()}")); sock=runtime/"discord-ipc-0"
    dlog=Path.home()/".var/app/com.discordapp.Discord/config/discord/logs/renderer_js.log"
    cfg=load_config(config_path);mtime=config_path.stat().st_mtime_ns;clock=proc_clock();rpc=Rpc();arb=LogArbiter(dlog);notified=False;current=None
    selector=selectors.DefaultSelector();next_full_scan=0.0;next_active_check=0.0;next_housekeeping=0.0
    next_reconnect=0.0;reconnect_attempt=0;disconnect_logged=False;safe=False
    def unregister():
        if rpc.s:
            try:selector.unregister(rpc.s)
            except (KeyError,ValueError):pass
    def disconnect(reason,now,retry=True):
        nonlocal next_reconnect,reconnect_attempt,disconnect_logged
        unregister();rpc.close()
        if retry:
            delay=RECONNECT_DELAYS[min(reconnect_attempt,len(RECONNECT_DELAYS)-1)]
            reconnect_attempt+=1;next_reconnect=now+delay
        if reason and not disconnect_logged:log(f"Discord RPC disconnected: {reason}");disconnect_logged=True
    def update_interest():
        if rpc.s:selector.modify(rpc.s,rpc.selector_events())
    try:
        while True:
            now=time.monotonic()
            if now>=next_housekeeping:
                try:
                    nm=config_path.stat().st_mtime_ns
                    if nm!=mtime:
                        cfg=load_config(config_path);mtime=nm;next_full_scan=0.0;log("configuration reloaded")
                except Exception as e:
                    log(f"configuration reload failed; keeping previous configuration: {e}")
                    if not notified:notify(f"Configuration error: {e}. See journalctl --user -u {APP}.service");notified=True
                if current and now>=next_active_check:
                    if process_is_same(current[1],current[2]):next_active_check=now+ACTIVE_CHECK_SECONDS
                    else:current=None;next_full_scan=0.0
                if now>=next_full_scan:
                    current=detect(cfg,clock=clock);next_full_scan=now+cfg.poll;next_active_check=now+ACTIVE_CHECK_SECONDS
                try:
                    if not arb.valid:arb.initialize()
                    arb.update(rpc.aid);safe=True
                except Exception as e:
                    safe=False
                    if not notified:
                        log(f"fail-closed: cannot verify Discord RPC log: {e}")
                        notify(f"Cannot verify competing RPC clients: {e}. Presence is suppressed. See the user journal.");notified=True
                if safe:notified=False
                next_housekeeping=now+(ACTIVE_CHECK_SECONDS if current else cfg.poll)

            eligible=bool(safe and current and arb.external==0 and sock.exists())
            if eligible:
                g,pid,ticks,start=current
                if rpc.s and rpc.aid!=g.application_id:
                    disconnect(None,now,retry=False);next_reconnect=now;reconnect_attempt=0
                rpc.desired_start=start
                if not rpc.s and now>=next_reconnect:
                    try:
                        rpc.connect(sock,g.application_id);selector.register(rpc.s,rpc.selector_events())
                        reconnect_attempt=0;next_reconnect=0.0
                        if disconnect_logged:log("Discord RPC connected");disconnect_logged=False
                    except Exception as e:disconnect(str(e),now)
                if rpc.s and rpc.pending_nonce is None:
                    if rpc.confirmed_start!=rpc.desired_start:rpc.queue_activity(start,"publish",now);update_interest()
                    elif now>=rpc.next_refresh:rpc.queue_activity(start,"refresh",now);update_interest()
            else:
                rpc.desired_start=None
                if rpc.s:
                    if rpc.pending_nonce is not None and rpc.pending_kind!="clear":disconnect(None,now,retry=False)
                    elif rpc.confirmed_start is not None:
                        rpc.queue_activity(None,"clear",now);update_interest()
                    elif rpc.pending_nonce is None:disconnect(None,now,retry=False)
                reconnect_attempt=0;next_reconnect=0.0
            if once:return 0

            now=time.monotonic()
            if rpc.pending_nonce and now>=rpc.pending_deadline:disconnect("RPC response timed out",now,retry=eligible);continue
            deadlines=[next_housekeeping,next_full_scan]
            if current:deadlines.append(next_active_check)
            if eligible and not rpc.s:deadlines.append(next_reconnect)
            if rpc.pending_nonce:deadlines.append(rpc.pending_deadline)
            if rpc.s and rpc.confirmed_start==rpc.desired_start and rpc.pending_nonce is None:deadlines.append(rpc.next_refresh)
            events=selector.select(max(0.0,min(deadlines)-now))
            for key,mask in events:
                try:
                    if mask&selectors.EVENT_READ:
                        for result in rpc.read_ready(time.monotonic()):
                            if result=="published":log(("restored" if disconnect_logged else "published")+f" {current[0].name} (pid {current[1]})");disconnect_logged=False
                            elif result=="cleared":
                                log("cleared fallback presence" if arb.external==0 else "yielding to external Rich Presence client")
                                disconnect(None,time.monotonic(),retry=False);break
                    if rpc.s and mask&selectors.EVENT_WRITE:rpc.write_ready()
                    if rpc.s:update_interest()
                except (RpcDisconnected,RpcProtocolError,OSError) as e:disconnect(str(e),time.monotonic(),retry=eligible)
    finally:
        if rpc.s and rpc.confirmed_start is not None:
            try:
                rpc.s.settimeout(1);rpc.s.sendall(frame(OP_FRAME,{"cmd":"SET_ACTIVITY","args":{"pid":os.getpid(),"activity":None},"nonce":str(uuid.uuid4())}))
            except OSError:pass
        unregister();rpc.close();selector.close()

def main():
    p=argparse.ArgumentParser();p.add_argument("--config",type=Path,default=Path.home()/".config/discord-game-presence/config.toml");p.add_argument("--check-config",action="store_true");p.add_argument("--once",action="store_true");a=p.parse_args()
    try:
        if a.check_config:print(f"valid: {len(load_config(a.config).games)} game(s)");return 0
        return run(a.config,a.once)
    except Exception as e:print(f"{APP}: {e}",file=sys.stderr);return 1
if __name__=="__main__":raise SystemExit(main())
