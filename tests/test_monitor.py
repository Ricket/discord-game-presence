import tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
from discord_game_presence import LogArbiter,detect,load_config,frame,process_basenames,process_is_same

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
 def test_arbiter_ignores_idle_handshake(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"renderer_js.log"
   p.write_text("Socket Opened: 458 402572971681644545 (active: 1)\nSocket Emit: 458 [object Object]\n")
   a=LogArbiter(p);a.initialize();a.update(None);self.assertEqual(a.external,0)
   with p.open("a") as f:f.write("Socket Message: 458 [object Object]\n")
   a.update(None);self.assertEqual(a.external,1)
if __name__=="__main__":unittest.main()
