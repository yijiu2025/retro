import sys
bad=[p for p in sys.path if 'shim' in p.lower() or 'ChemScript' in p]
for p in bad:
    try: sys.path.remove(p)
    except: pass
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors, Descriptors

base = "OC1=CC=CC2=C1CCC2N3C({X}=C(N){X}=C4NC5CC5)=C4N=C3"
print("骨架: ", base.format(X="<原子>"))
print("-" * 72)
for el in ["F", "Cl", "Br", "I", "O", "S", "N", "C"]:
    smi = base.format(X=el)
    m = Chem.MolFromSmiles(smi)
    flag = "OK " if m else "FAIL"
    extra = ""
    if m:
        extra = "%s  MW=%.1f" % (rdMolDescriptors.CalcMolFormula(m), Descriptors.MolWt(m))
    print("%s  X=%-3s %s" % (flag, el, extra))
    if not m:
        print("      ->", Chem.MolFromSmiles(smi, sanitize=False))
