import struct,tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
from discord_game_presence import MAX,OP_CLOSE,OP_FRAME,OP_PING,OP_PONG,RPC_SAFETY_REFRESH_SECONDS,LogArbiter,Rpc,RpcDisconnected,RpcProtocolError,detect,load_config,frame,process_basenames,process_is_same,raw_frame

class FakeSocket:
 def __init__(self):self.incoming=[];self.sent=bytearray();self.send_limit=None;self.block_send=False
 def feed(self,data):self.incoming.append(data)
 def sendall(self,data):self.feed(data)
 def recv(self,size):
  if not self.incoming:raise BlockingIOError
  data=self.incoming.pop(0)
  if len(data)>size:self.incoming.insert(0,data[size:]);return data[:size]
  return data
 def send(self,data):
  if self.block_send:raise BlockingIOError
  size=len(data) if self.send_limit is None else min(len(data),self.send_limit)
  self.sent.extend(data[:size]);return size
 def close(self):pass

class Tests(unittest.TestCase):
 def test_config(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"c.toml";p.write_text('poll_interval_seconds=5\n[[games]]\nname="X"\napplication_id="123"\nprocess_names=["X.exe"]\n')
   c=load_config(p);self.assertEqual(c.games[0].process_names,("x.exe",))
 def test_frame(self):
  b=frame(0,{"v":1});self.assertEqual(int.from_bytes(b[:4],"little"),0);self.assertEqual(int.from_bytes(b[4:8],"little"),len(b)-8)
 def test_invalid(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"c.toml";p.write_text('games=[]');self.assertRaises(ValueError,load_config,p)
 def test_windows_path_basename(self):
  value=r"S:\steamapps\common\rocketleague\Binaries\Win64\RocketLeague.exe"
  self.assertEqual(value.replace("\\","/").rsplit("/",1)[-1].casefold(),"rocketleague.exe")
 def test_native_path_with_spaces(self):
  cmd=b"/games/Tabletop Simulator/Tabletop Simulator.x86_64\x00-monitor\x003\x00"
  self.assertIn("tabletop simulator.x86_64",process_basenames("Tabletop Simula\n",cmd))
 def make_proc(self,root,pid,comm,cmdline=b"",ticks=100):
  p=root/str(pid);p.mkdir();(p/"comm").write_text(comm+"\n")
  if cmdline is not None:(p/"cmdline").write_bytes(cmdline)
  fields=["S"]+["0"]*18+[str(ticks)]+["0"]*3
  (p/"stat").write_text(f"{pid} ({comm}) "+" ".join(fields))
 def test_detect_preserves_configured_priority(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/"stat").write_text("btime 1000\n")
   cfg_path=root/"c.toml"
   cfg_path.write_text('[[games]]\nname="Main"\napplication_id="1"\nprocess_names=["MainGame.exe"]\n[[games]]\nname="Side"\napplication_id="2"\nprocess_names=["SideGame.exe"]\n')
   self.make_proc(root,20,"SideGame.exe",ticks=200)
   self.make_proc(root,30,"MainGame.exe",ticks=300)
   game,pid,ticks,start=detect(load_config(cfg_path),root,(1000,100))
   self.assertEqual((game.name,pid,ticks,start),("Main",30,300,1003))
 def test_detects_short_proton_launcher_alias_without_cmdline(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/"stat").write_text("btime 1000\n")
   cfg_path=root/"c.toml"
   cfg_path.write_text('[[games]]\nname="Far Far West"\napplication_id="1"\nprocess_names=["FarFarWest.exe","FarFarWest-Win64-Shipping.exe"]\n')
   self.make_proc(root,10,"FarFarWest.exe",cmdline=None,ticks=400)
   game,pid,_,_=detect(load_config(cfg_path),root,(1000,100))
   self.assertEqual((game.name,pid),("Far Far West",10))
 def test_detects_truncated_proton_launcher_alias_from_cmdline(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/"stat").write_text("btime 1000\n")
   cfg_path=root/"c.toml"
   cfg_path.write_text('[[games]]\nname="Expedition 33"\napplication_id="1"\nprocess_names=["Expedition33_Steam.exe","SandFall-Win64-Shipping.exe"]\n')
   self.make_proc(root,10,"Expedition33_St",b"S:\\games\\Expedition33_Steam.exe\0",ticks=400)
   game,pid,_,_=detect(load_config(cfg_path),root,(1000,100))
   self.assertEqual((game.name,pid),("Expedition 33",10))
 def test_unrelated_process_does_not_need_cmdline(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/"stat").write_text("btime 1000\n")
   cfg_path=root/"c.toml";cfg_path.write_text('[[games]]\nname="X"\napplication_id="1"\nprocess_names=["VeryLongGameName.exe"]\n')
   self.make_proc(root,10,"unrelated",cmdline=None)
   self.assertIsNone(detect(load_config(cfg_path),root,(1000,100)))
 def test_truncated_comm_reads_windows_argv(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/"stat").write_text("btime 1000\n")
   cfg_path=root/"c.toml";cfg_path.write_text('[[games]]\nname="X"\napplication_id="1"\nprocess_names=["VeryLongGameName.exe"]\n')
   self.make_proc(root,10,"VeryLongGameNam",b"S:\\games\\VeryLongGameName.exe\0",400)
   game,pid,_,_=detect(load_config(cfg_path),root,(1000,100))
   self.assertEqual((game.name,pid),("X",10))
 def test_process_identity_rejects_pid_reuse(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);self.make_proc(root,10,"Game",ticks=200)
   self.assertTrue(process_is_same(10,200,root))
   self.assertFalse(process_is_same(10,201,root))
 def rpc_pair(self):
  connection=FakeSocket();rpc=Rpc();rpc.s=connection;rpc.aid="123"
  return rpc,connection
 def test_rpc_fragmented_frame(self):
  rpc,server=self.rpc_pair();rpc.desired_start=10;rpc.queue_activity(10,"publish",1)
  nonce=rpc.pending_nonce;data=frame(OP_FRAME,{"nonce":nonce,"data":{}})
  server.sendall(data[:3]);self.assertEqual(rpc.read_ready(2),[])
  server.sendall(data[3:9]);self.assertEqual(rpc.read_ready(2),[])
  server.sendall(data[9:]);self.assertEqual(rpc.read_ready(2),["published"])
  self.assertEqual(rpc.confirmed_start,10)
  self.assertEqual(rpc.next_refresh,2+RPC_SAFETY_REFRESH_SECONDS)
 def test_rpc_multiple_frames_and_exact_ping_pong(self):
  rpc,server=self.rpc_pair();body=b'{"probe":"unchanged"}'
  server.sendall(raw_frame(OP_PING,body)+raw_frame(OP_PONG,b"{}"))
  self.assertEqual(rpc.read_ready(1),[]);self.assertEqual(bytes(rpc.tx),raw_frame(OP_PONG,body))
 def test_rpc_partial_frame_is_retained(self):
  rpc,server=self.rpc_pair();one=frame(OP_FRAME,{"evt":"NOTICE"});two=frame(OP_FRAME,{"evt":"LATER"})
  server.sendall(one+two[:10]);self.assertEqual(rpc.read_ready(1),[]);self.assertEqual(bytes(rpc.rx),two[:10])
  server.sendall(two[10:]);self.assertEqual(rpc.read_ready(1),[]);self.assertFalse(rpc.rx)
 def test_rpc_nonmatching_nonce_cannot_confirm(self):
  rpc,server=self.rpc_pair();rpc.desired_start=10;rpc.queue_activity(10,"publish",1)
  server.sendall(frame(OP_FRAME,{"nonce":"old","data":{}}));self.assertEqual(rpc.read_ready(2),[])
  self.assertIsNone(rpc.confirmed_start);self.assertIsNotNone(rpc.pending_nonce)
 def test_rpc_error_does_not_confirm(self):
  rpc,server=self.rpc_pair();rpc.desired_start=10;rpc.queue_activity(10,"publish",1);nonce=rpc.pending_nonce
  server.sendall(frame(OP_FRAME,{"nonce":nonce,"evt":"ERROR","data":{"message":"no"}}))
  self.assertRaises(RpcProtocolError,rpc.read_ready,2);self.assertIsNone(rpc.confirmed_start)
 def test_rpc_close_and_eof_disconnect(self):
  rpc,server=self.rpc_pair();server.sendall(frame(OP_CLOSE,{"code":4000,"message":"bad"}))
  self.assertRaises(RpcDisconnected,rpc.read_ready,1)
  rpc2,server2=self.rpc_pair();server2.feed(b"");self.assertRaises(RpcDisconnected,rpc2.read_ready,1)
 def test_rpc_rejects_oversized_and_malformed_frames(self):
  rpc,server=self.rpc_pair();server.sendall(struct.pack("<II",OP_FRAME,MAX+1))
  self.assertRaises(RpcProtocolError,rpc.read_ready,1)
  rpc2,server2=self.rpc_pair();server2.sendall(raw_frame(OP_FRAME,b"not json"))
  self.assertRaises(RpcProtocolError,rpc2.read_ready,1)
 def test_rpc_clear_acknowledgement(self):
  rpc,server=self.rpc_pair();rpc.confirmed_start=10;rpc.desired_start=None;rpc.queue_activity(None,"clear",1);nonce=rpc.pending_nonce
  server.sendall(frame(OP_FRAME,{"nonce":nonce,"data":{}}));self.assertEqual(rpc.read_ready(2),["cleared"])
  self.assertIsNone(rpc.confirmed_start)
 def test_rpc_write_drains_buffer(self):
  rpc,server=self.rpc_pair();rpc.tx.extend(b"payload");server.send_limit=3;rpc.write_ready()
  self.assertEqual(bytes(server.sent),b"payload");self.assertFalse(rpc.tx)
 def test_rpc_blocked_write_retains_buffer(self):
  rpc,server=self.rpc_pair();rpc.tx.extend(b"payload");server.block_send=True;rpc.write_ready()
  self.assertEqual(bytes(rpc.tx),b"payload");self.assertFalse(server.sent)
 def test_rpc_rejects_unexpected_opcode(self):
  rpc,server=self.rpc_pair();server.sendall(raw_frame(OP_PONG+1,b"{}"))
  self.assertRaises(RpcProtocolError,rpc.read_ready,1)
 def test_rpc_activity_is_not_queued_while_pending(self):
  rpc,_=self.rpc_pair();self.assertTrue(rpc.queue_activity(10,"publish",1))
  self.assertFalse(rpc.queue_activity(10,"refresh",2))
 def test_arbiter_ignores_idle_handshake(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"renderer_js.log"
   p.write_text("Socket Opened: 458 402572971681644545 (active: 1)\nSocket Emit: 458 [object Object]\n")
   a=LogArbiter(p);a.initialize();a.update(None);self.assertEqual(a.external,0)
   with p.open("a") as f:f.write("Socket Message: 458 [object Object]\n")
   a.update(None);self.assertEqual(a.external,1)
if __name__=="__main__":unittest.main()
