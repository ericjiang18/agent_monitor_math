"""Reproduce the public worked example without model calls or user data."""
from fractions import Fraction
from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / 'agent_monitor/examples/ErdosStraus.lean'
check = subprocess.run(['lean', str(source)], capture_output=True, text=True, check=True)
assert 'does not depend on any axioms' in check.stdout
witnesses = []
for n in range(2, 501):
    found = None
    for x in range(n // 4 + 1, 3 * n // 4 + 1):
        a, b = 4*x-n, n*x
        for y in range(max(x, b//a+1), 2*b//a+1):
            num, den = b*y, a*y-b
            if den > 0 and num % den == 0:
                found = (x, y, num//den)
                break
        if found:
            break
    assert found and sum((Fraction(1, d) for d in found), Fraction()) == Fraction(4, n)
    witnesses.append([n, *found])

nodes = [{'id':'witness','label':'UnitFractionWitness','kind':'definition','lean_kind':'def','statement':'Positive denominators and the exact cross-multiplied unit-fraction identity.','status':'definition'}]
for key, n, xyz in [('two',2,'1 2 2'),('three',3,'1 4 12'),('five',5,'2 4 20'),('seven',7,'2 28 28'),('thirteen',13,'4 26 52')]:
    nodes.append({'id':key,'label':'witness_'+key,'kind':'lemma','lean_kind':'theorem','statement':f'UnitFractionWitness {n} {xyz}','status':'kernel checked'})
nodes.append({'id':'cases','label':'checked_cases','kind':'conclusion','lean_kind':'theorem','statement':'Conjunction of the five finite witnesses. This theorem does not imply ErdosStraus.','status':'kernel checked'})
nodes.append({'id':'open','label':'ErdosStraus','kind':'claim','lean_kind':'def','statement':'For every n ≥ 2, there exist three positive unit fractions summing to 4/n. No proof supplied.','status':'open goal'})
edges = [{'from':'witness','to':key,'label':'unfolds'} for key in ['two','three','five','seven','thirteen']]+[{'from':key,'to':'cases','label':'uses'} for key in ['two','three','five','seven','thirteen']]
run = {
'version':1,'type':'example','title':'Erdős–Straus: witnesses, checks, and an open goal',
'status':'Partial results · conjecture unresolved','engine':'Worked research example',
'notice':'Curated demonstration, not a captured multi-agent execution or a claimed solution. The exact-arithmetic search and five Lean witnesses were actually run; the research roles illustrate how a project can divide the work. No model usage or costs are invented.',
'problem':'For every integer n ≥ 2, can 4/n be written as 1/x + 1/y + 1/z for positive integers x, y, z? Investigate partial cases, verify finite evidence, and keep the general conjecture open.',
'source_url':'https://www.erdosproblems.com/242',
'proof':'''PARTIAL RESULT: EVEN DENOMINATORS
For n = 2m, take x = m and y = z = 2m. Then 1/m + 1/(2m) + 1/(2m) = 2/m = 4/n. This establishes the even case by elementary algebra; this general identity is not formalized in the accompanying Lean file.

EXACT COMPUTATION
The reproducible Python search found positive integer witnesses for every n from 2 through 500. Each sum was checked using exact rational arithmetic. This is finite evidence, not a proof for all n.

LEAN CHECK
Five explicit witnesses (n = 2, 3, 5, 7, 13) and their conjunction were checked with Lean 4.14.0. The kernel reports that checked_cases depends on no axioms. The formal file checks the equivalent cross-multiplied natural-number identity with positive denominators.

OPEN WORK
The general odd case remains unresolved here. No edge leads from the finite-witness theorem to the general conjecture: that implication has not been established.''',
'agents':[
 {'id':'scope','label':'Research planner','status':'Illustrative role','summary':'Separate the universal conjecture, the even case, and finite computational evidence. Preserve the source problem and its quantifiers.'},
 {'id':'algebra','label':'Algebra explorer','status':'Partial result','summary':'Derive the even-denominator family (m, 2m, 2m). This leaves odd denominators open.'},
 {'id':'search','label':'Exact-arithmetic search','status':'Executed locally','summary':f'Checked {len(witnesses)} denominators, n = 2…500. All had exact positive witnesses; no floating-point comparisons.'},
 {'id':'formal','label':'Lean verifier','status':'Executed locally','summary':check.stdout.strip()},
 {'id':'review','label':'Critical reviewer','status':'Illustrative role','summary':'Finite checks do not establish the universal statement. The general conjecture is intentionally disconnected from the certified finite-case DAG.'}],
'formal_graph':{'title':'Certified finite cases and a separate open goal','nodes':nodes,'edges':edges},
'informal_graph':{'title':'Research plan','nodes':[{'id':'scope','label':'Scope the conjecture','kind':'assumption','statement':'n ≥ 2; three positive denominators.'},{'id':'even','label':'Even-denominator family','kind':'lemma','statement':'For n=2m use (m,2m,2m).'}, {'id':'finite','label':'Finite exact search','kind':'case','statement':'Check n=2…500 using exact rational arithmetic.'},{'id':'review','label':'Review the remaining gap','kind':'conclusion','statement':'The general odd case remains open.'}], 'edges':[{'from':'scope','to':'even','label':'case split'},{'from':'scope','to':'finite','label':'experiment'},{'from':'even','to':'review','label':'partial result'},{'from':'finite','to':'review','label':'finite evidence'}]},
'lean_source':source.read_text(),'verification':{'command':'lean agent_monitor/examples/ErdosStraus.lean','version':subprocess.check_output(['lean','--version'],text=True).strip(),'result':check.stdout.strip(),'checked_denominators':499},
'witnesses':witnesses,
}
(ROOT/'agent_monitor/examples/erdos-straus.json').write_text(json.dumps(run,ensure_ascii=False,indent=2)+'\n')
print('Rebuilt example: 499 exact arithmetic checks; five Lean witnesses; no axioms.')
