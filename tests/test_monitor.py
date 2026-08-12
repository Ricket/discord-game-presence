import tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
from discord_game_presence import Config,Game,LogArbiter,Rpc,detect,load_config,frame,process_basenames

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
 def test_arbiter_ignores_idle_handshake(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"renderer_js.log"
   p.write_text("Socket Opened: 458 402572971681644545 (active: 1)\nSocket Emit: 458 [object Object]\n")
   a=LogArbiter(p);a.initialize();a.update(None);self.assertEqual(a.external,0)
   with p.open("a") as f:f.write("Socket Message: 458 [object Object]\n")
   a.update(None);self.assertEqual(a.external,1)
if __name__=="__main__":unittest.main()
