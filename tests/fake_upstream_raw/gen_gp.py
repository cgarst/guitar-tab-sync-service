"""Same stand-in as fake_upstream/gen_gp.py."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "fake_upstream" / "gen_gp.py"
_spec = importlib.util.spec_from_file_location("fake_upstream_gen_gp", _path)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
globals().update({k: v for k, v in vars(_base).items() if not k.startswith("__")})
