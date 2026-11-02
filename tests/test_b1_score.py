import math
from routing.update import compute_appearance_log_lik

def test_b1_score_formula():
    # a) strong sighting raises score (positive log odds)
    assert compute_appearance_log_lik(0.72) > 0.0
    
    # b) weak sighting doesn't raise score (negative log odds)
    assert compute_appearance_log_lik(0.3) < 0.0
    
    # a = 0.5 -> 0
    assert abs(compute_appearance_log_lik(0.5)) < 1e-6
    
    # c) a=1.0 does not produce infinity
    v = compute_appearance_log_lik(1.0)
    assert not math.isinf(v)
    assert v == compute_appearance_log_lik(0.99) # clipped
