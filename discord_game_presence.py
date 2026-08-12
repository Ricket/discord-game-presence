#!/usr/bin/env python3
"""Privacy-preserving fallback Discord game presence monitor."""
from __future__ import annotations
import argparse, json, os, re, selectors, signal, socket, struct, subprocess, sys, time, tomllib, uuid
from dataclasses import dataclass
from pathlib import Path

APP="discord-game-presence"; MAX=16*1024*1024
@dataclass(frozen=True)
class Game:
    name:str; application_id:str; process_names:tuple[str,...]
@dataclass(frozen=True)
class Config:
    poll:float; games:tuple[Game,...]

def load_config(path:Path)->Config:
    with path.open("rb") as f: raw=tomllib.load(f)
    poll=float(raw.get("poll_interval_seconds",5))
    if not 1 <= poll <= 300: raise ValueError("poll_interval_seconds must be between 1 and 300")
    games=[]
    for i,g in enumerate(raw.get("games",[]),1):
        aid=str(g.get("application_id","")); names=tuple(str(x).casefold() for x in g.get("process_names",[]))
        if not g.get("name") or not aid.isdigit() or not names: raise ValueError(f"invalid games entry {i}")
        games.append(Game(str(g["name"]),aid,names))
    if not games: raise ValueError("at least one [[games]] entry is required")
    return Config(poll,tuple(games))

def proc_start(pid:int)->int:
    fields=Path(f"/proc/{pid}/stat").read_text().rsplit(") ",1)[1].split()
    ticks=int(fields[19]); boot=int(Path("/proc/stat").read_text().split("btime ",1)[1].splitlines()[0])
    return boot + ticks//os.sysconf("SC_CLK_TCK")
def process_basenames(comm:str,cmdline:bytes)->set[str]:
    argv=[x.decode(errors="replace") for x in cmdline.split(b"\0") if x]
    return {comm.strip().casefold()}|{x.replace("\\","/").rsplit("/",1)[-1].casefold() for x in argv}
def detect(config:Config):
    found={}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit(): continue
        try:
            comm=(p/"comm").read_text()
            # Proton exposes Windows argv entries (for example
            # S:\\steamapps\\...\\RocketLeague.exe), which pathlib on Linux does
            # not recognize as paths. Normalize both separator styles while
            # preserving spaces inside a native executable's argv[0].
            bases=process_basenames(comm,(p/"cmdline").read_bytes())
            for g in config.games:
                if any(n in bases for n in g.process_names): found.setdefault(g,(int(p.name),proc_start(int(p.name))))
        except (FileNotFoundError,PermissionError,ProcessLookupError,ValueError): pass
    for g in config.games:
        if g in found:return g,*found[g]
    return None

def frame(op:int,data:dict)->bytes:
    b=json.dumps(data,separators=(",",":")).encode(); return struct.pack("<II",op,len(b))+b
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

class Rpc:
    def __init__(self):self.s=None;self.aid=None;self.last_activity=0.0
    def close(self):
        if self.s:
            try:self.s.close()
            except OSError:pass
        self.s=None;self.aid=None;self.last_activity=0.0
    def connect(self,path:Path,aid:str):
        self.close(); s=socket.socket(socket.AF_UNIX);s.settimeout(3);s.connect(str(path));s.sendall(frame(0,{"v":1,"client_id":aid}));op,msg=recv(s)
        if op!=1 or msg.get("evt")!="READY":s.close();raise ConnectionError(f"RPC rejected: {msg}")
        self.s=s;self.aid=aid
    def activity(self,start:int|None):
        nonce=str(uuid.uuid4()); self.s.sendall(frame(1,{"cmd":"SET_ACTIVITY","args":{"pid":os.getpid(),"activity":None if start is None else {"timestamps":{"start":start},"instance":True}},"nonce":nonce}))
        while True:
            op,msg=recv(self.s)
            if op==3:self.s.sendall(frame(4,msg));continue
            if msg.get("nonce")==nonce:
                if msg.get("evt")=="ERROR":raise RuntimeError(str(msg))
                self.last_activity=time.monotonic()
                return

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
    cfg=load_config(config_path);mtime=config_path.stat().st_mtime_ns; rpc=Rpc();arb=LogArbiter(dlog);notified=False;current=None
    try:
        while True:
            try:
                nm=config_path.stat().st_mtime_ns
                if nm!=mtime: cfg=load_config(config_path);mtime=nm;log("configuration reloaded")
            except Exception as e:
                log(f"configuration reload failed; keeping previous configuration: {e}")
                if not notified:notify(f"Configuration error: {e}. See journalctl --user -u {APP}.service");notified=True
            game=detect(cfg)
            try:
                if not arb.valid:arb.initialize()
                arb.update(rpc.aid)
                safe=True
            except Exception as e:
                safe=False;rpc.close()
                if not notified:notify(f"Cannot verify competing RPC clients: {e}. Presence is suppressed. See the user journal.");notified=True
                log(f"fail-closed: cannot verify Discord RPC log: {e}")
            if safe:
                notified=False
                if game and arb.external==0 and sock.exists():
                    g,pid,start=game
                    try:
                        if rpc.aid!=g.application_id:
                            rpc.connect(sock,g.application_id);rpc.activity(start);log(f"published {g.name} (pid {pid})")
                        elif time.monotonic()-rpc.last_activity>=30:
                            # A normal activity refresh verifies the connection
                            # and restores presence after a Discord restart.
                            rpc.activity(start)
                        current=g
                    except Exception as e:rpc.close();log(f"Discord unavailable: {e}")
                else:
                    if rpc.s:
                        try:rpc.activity(None)
                        except Exception:pass
                        rpc.close();log("cleared fallback presence" if not arb.external else "yielding to external Rich Presence client")
                    current=None
            if once:return 0
            time.sleep(cfg.poll)
    finally:
        if rpc.s:
            try:rpc.activity(None)
            except Exception:pass
        rpc.close()

def main():
    p=argparse.ArgumentParser();p.add_argument("--config",type=Path,default=Path.home()/".config/discord-game-presence/config.toml");p.add_argument("--check-config",action="store_true");p.add_argument("--once",action="store_true");a=p.parse_args()
    try:
        if a.check_config:print(f"valid: {len(load_config(a.config).games)} game(s)");return 0
        return run(a.config,a.once)
    except Exception as e:print(f"{APP}: {e}",file=sys.stderr);return 1
if __name__=="__main__":raise SystemExit(main())
