"""Actual engine/delivery in temporary database, no Telegram network."""
import asyncio,inspect,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import synthetic_pipeline as sp
from critical_rejection_research import OUT,save,liquidity
original=sp.fixture
def sequence(now):
    d=original(now)
    d.loc[338,'low']=98.5;d.loc[339,'high']=102.;d.loc[343,'low']=98.
    return d
source=inspect.getsource(sp.run).replace('cfg.min_signal_score=65','cfg.min_signal_score=80')
ns=dict(sp.run.__globals__);ns['fixture']=sequence
exec(compile(source,'<isolated_80_fixture>','exec'),ns)
if __name__=='__main__':
    result=asyncio.run(ns['run']())
    result['threshold']=80
    import time
    result['preceding_typed_sweep']=liquidity(sequence(time.time()).iloc[:345],True)
    assert result['preceding_typed_sweep']
    save('synthetic_80.json',result);print(json.dumps(result))
