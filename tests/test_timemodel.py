import pytest
import math
from routing.timemodel import (
    time_likelihood, dwell_likelihood, edge_sigma,
    lognormal_pdf, lognormal_peak,
    edge_time_likelihood, blind_dwell_likelihood, outside_dwell_likelihood,
)
from routing.contracts import EdgeDef, BlindNodeDef, OutsideDef


class TestEdgeSigma:
    def test_basic(self):
        # σ = ln(60/15) / 1.645 = ln(4) / 1.645 ≈ 0.843
        s = edge_sigma(15.0, 60.0)
        assert abs(s - math.log(4) / 1.645) < 1e-10


class TestTimeLikelihood:
    def test_hard_gate(self):
        # dt < t_min → 0
        assert time_likelihood(3.0, t_min=5.0, t_typ=10.0, t_max=30.0) == 0.0
    
    def test_at_t_min(self):
        # dt == t_min → should be > 0 (not hard gated)
        lik = time_likelihood(5.0, t_min=5.0, t_typ=10.0, t_max=30.0)
        assert lik > 0.0
    
    def test_peak_near_mode(self):
        # Near the mode, likelihood should be close to 1.0
        # Mode = exp(mu - sigma^2)
        t_typ, t_max = 10.0, 30.0
        sigma = edge_sigma(t_typ, t_max)
        mu = math.log(t_typ)
        mode = math.exp(mu - sigma * sigma)
        lik = time_likelihood(mode, t_min=1.0, t_typ=t_typ, t_max=t_max)
        assert abs(lik - 1.0) < 1e-6
    
    def test_lateness_penalty(self):
        # Very late arrival → low but non-zero
        lik = time_likelihood(1000.0, t_min=5.0, t_typ=10.0, t_max=30.0)
        assert 0.0 < lik < 0.1
    
    def test_at_typical(self):
        # At t_typ, likelihood should be high (close to 1 but slightly less)
        lik = time_likelihood(10.0, t_min=5.0, t_typ=10.0, t_max=30.0)
        assert lik > 0.8


class TestDwellLikelihood:
    def test_positive(self):
        lik = dwell_likelihood(30.0, median_s=30.0, sigma=0.8)
        assert lik > 0.5  # Near median (peak is at mode, not median)
    
    def test_zero_dt(self):
        assert dwell_likelihood(0.0, median_s=30.0, sigma=0.8) == 0.0
    
    def test_heavy_tail(self):
        # Even very long dwell should be > 0
        lik = dwell_likelihood(500.0, median_s=30.0, sigma=0.8)
        assert lik > 0.0


class TestEdgeConvenience:
    def test_edge_time(self):
        edge = EdgeDef(id='e1', source='A', target='B', t_min=5, t_typ=10, t_max=30)
        assert edge_time_likelihood(3.0, edge) == 0.0
        assert edge_time_likelihood(10.0, edge) > 0.5
    
    def test_blind_dwell(self):
        node = BlindNodeDef(id='bn1', dwell_median_s=20, dwell_sigma=0.5)
        assert blind_dwell_likelihood(20.0, node) > 0.5
    
    def test_outside_dwell(self):
        outside = OutsideDef(entrances={'A': 1.0}, dwell_median_s=300, dwell_sigma=1.0)
        assert outside_dwell_likelihood(300.0, outside) > 0.5
