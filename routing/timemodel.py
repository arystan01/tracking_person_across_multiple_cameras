from __future__ import annotations
import math
from .contracts import EdgeDef, BlindNodeDef, OutsideDef


def edge_sigma(t_typ: float, t_max: float) -> float:
    """Compute σ for lognormal: σ = ln(t_max / t_typ) / 1.645"""
    # Site validation permits t_typ == t_max.  Treat that as a very narrow,
    # but still numerically valid distribution instead of dividing by zero.
    return max(math.log(t_max / t_typ) / 1.645, 1e-6)


def lognormal_pdf(t: float, mu: float, sigma: float) -> float:
    """Standard lognormal PDF."""
    if t <= 0:
        return 0.0
    z = (math.log(t) - mu) / sigma
    return (1.0 / (t * sigma * math.sqrt(2 * math.pi))) * math.exp(-0.5 * z * z)


def lognormal_peak(mu: float, sigma: float) -> float:
    """Peak (max) value of the lognormal PDF."""
    mode = math.exp(mu - sigma * sigma)
    return lognormal_pdf(mode, mu, sigma)


def time_likelihood(dt_seconds: float, t_min: float, t_typ: float, t_max: float) -> float:
    """Compute peak-normalized time likelihood for an edge transition.
    
    Returns 0.0 if dt < t_min (hard gate).
    Returns peak-normalized lognormal value otherwise.
    """
    if dt_seconds < t_min:
        return 0.0  # Hard gate
    sigma = edge_sigma(t_typ, t_max)
    mu = math.log(t_typ)  # median = t_typ, so mu = ln(t_typ)
    peak = lognormal_peak(mu, sigma)
    if peak <= 0:
        return 0.0
    return lognormal_pdf(dt_seconds, mu, sigma) / peak


def dwell_likelihood(dt_seconds: float, median_s: float, sigma: float) -> float:
    """Compute peak-normalized dwell time likelihood for blind nodes / OUTSIDE.
    
    No hard gate — just the lognormal.
    """
    if dt_seconds <= 0:
        return 0.0
    mu = math.log(median_s)
    peak = lognormal_peak(mu, sigma)
    if peak <= 0:
        return 0.0
    return lognormal_pdf(dt_seconds, mu, sigma) / peak


def lognormal_cdf(t: float, median: float, sigma: float) -> float:
    """CDF of a lognormal distribution parameterised by its median."""
    if t <= 0:
        return 0.0
    sigma = max(sigma, 1e-6)
    z = (math.log(t) - math.log(median)) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def arrival_mass(t_from_s: float, t_to_s: float, edge: EdgeDef) -> float:
    """Probability mass in an edge arrival interval, respecting t_min."""
    start = max(t_from_s, edge.t_min)
    end = max(t_to_s, start)
    if end <= start:
        return 0.0
    sigma = edge_sigma(edge.t_typ, edge.t_max)
    # The hard gate truncates the distribution below t_min. Renormalise the
    # remaining probability so full post-gate coverage approaches one.
    gate_cdf = lognormal_cdf(edge.t_min, edge.t_typ, sigma)
    denom = max(1.0 - gate_cdf, 1e-12)
    mass = (
        lognormal_cdf(end, edge.t_typ, sigma)
        - lognormal_cdf(start, edge.t_typ, sigma)
    ) / denom
    return min(max(mass, 0.0), 1.0)


def edge_time_likelihood(dt_seconds: float, edge: EdgeDef) -> float:
    """Time likelihood for traversing an edge."""
    return time_likelihood(dt_seconds, edge.t_min, edge.t_typ, edge.t_max)


def blind_dwell_likelihood(dt_seconds: float, node: BlindNodeDef) -> float:
    """Dwell likelihood for a blind node."""
    return dwell_likelihood(dt_seconds, node.dwell_median_s, node.dwell_sigma)


def outside_dwell_likelihood(dt_seconds: float, outside: OutsideDef) -> float:
    """Dwell likelihood for OUTSIDE."""
    return dwell_likelihood(dt_seconds, outside.dwell_median_s, outside.dwell_sigma)
