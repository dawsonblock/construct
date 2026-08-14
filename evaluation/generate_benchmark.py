from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from construction_ai.evaluation.benchmark import run

if __name__=='__main__':
    r=run(n_cases=1000)
    print(r)
