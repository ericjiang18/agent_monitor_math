import Mathlib.Data.Real.Irrational
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.NormNum

/-- The positive real square root of 8 is irrational.
Cites: self-derived — computes Real.sqrt 8 = 2 * Real.sqrt 2 and derives a rational representative for Real.sqrt 2 from one for Real.sqrt 8;
Mathlib Real.sqrt and irrational_sqrt_two — real square-root algebra and irrationality of Real.sqrt 2 used as the base case -/
theorem main : Irrational (Real.sqrt 8) := by
  intro h
  rcases h with ⟨q, hq⟩
  have hsqrt : Real.sqrt 8 = 2 * Real.sqrt 2 := by
    have h4 : (4 : ℝ) = 2 ^ 2 := by norm_num
    calc
      Real.sqrt 8 = Real.sqrt (4 * (2 : ℝ)) := by norm_num
      _ = Real.sqrt 4 * Real.sqrt 2 := by
        rw [Real.sqrt_mul (show (0 : ℝ) ≤ 4 by norm_num) 2]
      _ = 2 * Real.sqrt 2 := by
        rw [h4, Real.sqrt_sq (show (0 : ℝ) ≤ 2 by norm_num)]
  have hq' : (q : ℝ) = 2 * Real.sqrt 2 := by simpa [hsqrt] using hq
  have hhalf : (q : ℝ) / 2 = Real.sqrt 2 := by linarith
  exact irrational_sqrt_two ⟨q / 2, by simpa using hhalf⟩
