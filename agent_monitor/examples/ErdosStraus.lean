-- Exact finite witnesses, not a proof of the Erdos-Straus conjecture.
def UnitFractionWitness (n x y z : Nat) : Prop :=
  0 < x ∧ 0 < y ∧ 0 < z ∧
  4 * (x * y * z) = n * (y * z + x * z + x * y)

def ErdosStraus : Prop :=
  ∀ n : Nat, 2 ≤ n → ∃ x y z : Nat, UnitFractionWitness n x y z

theorem witness_two : UnitFractionWitness 2 1 2 2 := by unfold UnitFractionWitness; decide
theorem witness_three : UnitFractionWitness 3 1 4 12 := by unfold UnitFractionWitness; decide
theorem witness_five : UnitFractionWitness 5 2 4 20 := by unfold UnitFractionWitness; decide
theorem witness_seven : UnitFractionWitness 7 2 28 28 := by unfold UnitFractionWitness; decide
theorem witness_thirteen : UnitFractionWitness 13 4 26 52 := by unfold UnitFractionWitness; decide

theorem checked_cases :
    UnitFractionWitness 2 1 2 2 ∧ UnitFractionWitness 3 1 4 12 ∧
    UnitFractionWitness 5 2 4 20 ∧ UnitFractionWitness 7 2 28 28 ∧
    UnitFractionWitness 13 4 26 52 :=
  ⟨witness_two, witness_three, witness_five, witness_seven, witness_thirteen⟩

#print axioms checked_cases
