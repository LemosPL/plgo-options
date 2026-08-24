import csv as _csv
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.interpolate import UnivariateSpline


def _target_profile_data_dir() -> Path:
    """Locate the data/ directory holding the built-in target-profile CSVs, whether
    run from the repo root, a subdir, or the /app image."""
    for cand in (Path("data"), Path("../data"), Path("../../data"),
                 Path(__file__).resolve().parents[2] / "data"):
        if cand.exists():
            return cand
    return Path("data")


def _user_target_profile_dir() -> Path:
    """Writable, persistent dir for user-created target profiles. Uses the same
    GCS-backed mount as the DB (DB_DIR) when set so curves survive restarts;
    falls back to the local data/ dir in dev."""
    db = os.environ.get("DB_DIR")
    return (Path(db) / "target_profiles") if db else _target_profile_data_dir()


def _target_profile_dirs() -> list[Path]:
    """Dirs to search for target profiles — user dir first so a user curve wins
    over a built-in of the same name."""
    dirs, seen = [], set()
    for d in (_user_target_profile_dir(), _target_profile_data_dir()):
        rp = str(d.resolve()) if d.exists() else str(d)
        if rp not in seen:
            seen.add(rp)
            dirs.append(d)
    return dirs


def _clean_currency(value) -> "float | None":
    """Parse a number that may be in accounting format — '$0.25', '(1,000,000)',
    '$1,234.5' — returning a float (parentheses = negative) or None if unparseable."""
    s = str(value).strip().replace("$", "").replace(",", "").replace(" ", "").strip()
    if not s or s == "-":
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def list_target_profiles(asset: str) -> list[dict]:
    """List target-profile CSVs for an asset (built-in + user-created), e.g.
    'ETH - target.csv'. Returns [{name, file, user}] sorted by name; a user curve
    of the same filename shadows a built-in one."""
    out: dict[str, dict] = {}
    for i, d in enumerate(_target_profile_dirs()):
        is_user = (i == 0 and d.resolve() != _target_profile_data_dir().resolve()) if d.exists() else False
        try:
            for p in sorted(d.glob(f"{asset} - *.csv")):
                out.setdefault(p.name, {"name": p.stem, "file": p.name, "user": is_user})
        except Exception:
            pass
    return sorted(out.values(), key=lambda r: r["name"])


def load_target_profile_file(filename: str, asset: str = "ETH") -> pd.DataFrame:
    """Load a target-profile CSV (Strike, Payoff columns) into the same smoothed
    DataFrame shape build_parametric_target_profile returns. Searches the user dir
    then the built-in data dir. Handles the clean ETH format and the FIL accounting
    format ('$0.25', '(1,000,000)')."""
    p = None
    from_user = False
    _user = _user_target_profile_dir()
    _data = _target_profile_data_dir()
    for d in _target_profile_dirs():
        cand = d / filename
        if cand.suffix.lower() == ".csv" and cand.exists() and cand.is_file() \
                and cand.resolve().is_relative_to(d.resolve()):
            p = cand
            try:
                from_user = (d.resolve() == _user.resolve() and _user.resolve() != _data.resolve())
            except Exception:
                from_user = False
            break
    if p is None:
        raise FileNotFoundError(f"Target profile not found: {filename}")

    with p.open(newline="") as f:
        rows = list(_csv.reader(f))
    strikes: list[float] = []
    payoffs: list[float] = []
    for row in rows[1:]:  # skip header
        if len(row) < 2:
            continue
        k = _clean_currency(row[0])
        v = _clean_currency(row[1])
        if k is None or v is None:
            continue
        strikes.append(k)
        payoffs.append(v)
    if len(strikes) < 2:
        raise ValueError(f"Target profile {filename} has fewer than 2 usable rows")

    df = pd.DataFrame({"Payoff($)": payoffs}, index=pd.Index(strikes, name="Strike($)")).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    if from_user:
        # User-saved curves are already the exact shape the user drew — don't
        # re-smooth (spline overshoot changes the numbers and can dip negative).
        return df
    try:
        return smooth_target_profile(df)
    except Exception:
        return df  # spline can fail on sparse/stepped profiles — use the raw curve


def save_target_profile(asset: str, name: str, points: list[dict]) -> str:
    """Persist a user-created target curve as '{ASSET} - {name}.csv' in the user
    profile dir (Strike($), Payoff($) columns). ``points`` is [{x, y}, ...]. Returns
    the filename, which then appears in list_target_profiles / loads via the loader."""
    safe = re.sub(r"[^A-Za-z0-9 _+.\-]", "", str(name or "")).strip()
    if not safe:
        raise ValueError("Invalid profile name")
    rows: list[tuple[float, float]] = []
    for pt in (points or []):
        try:
            x = float(pt.get("x")); y = float(pt.get("y"))
        except (TypeError, ValueError, AttributeError):
            continue
        if np.isfinite(x) and np.isfinite(y):
            rows.append((x, y))
    rows.sort(key=lambda t: t[0])
    # de-duplicate equal strikes (keep first)
    deduped: list[tuple[float, float]] = []
    for x, y in rows:
        if deduped and x <= deduped[-1][0]:
            continue
        deduped.append((x, y))
    if len(deduped) < 2:
        raise ValueError("A target profile needs at least 2 distinct points")

    d = _user_target_profile_dir()
    d.mkdir(parents=True, exist_ok=True)
    filename = f"{asset.upper()} - {safe}.csv"
    path = d / filename
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["Strike($)", "Payoff($)"])
        for x, y in deduped:
            w.writerow([x, y])
    os.replace(tmp, path)
    return filename


def delete_target_profile(asset: str, filename: str) -> None:
    """Delete a USER-created target profile. Refuses to delete the shipped
    built-in profiles (those live in the read-only data/ dir)."""
    ud = _user_target_profile_dir()
    dd = _target_profile_data_dir()
    up = ud / filename
    if up.suffix.lower() == ".csv" and up.exists() and up.is_file() \
            and up.resolve().is_relative_to(ud.resolve()):
        up.unlink()
        return
    # Not in the (writable) user dir — is it a shipped built-in?
    if (dd / filename).exists():
        raise ValueError("Built-in target profiles can't be deleted.")
    raise FileNotFoundError(f"Target profile not found: {filename}")


def shift_target_profile(
    target_profile: pd.DataFrame,
    current_spot: float,
    payoff_col: str = "Payoff($)",
) -> pd.DataFrame:
    """
    Homothetically scale the target profile's strike axis so that the minimum
    payoff occurs at current_spot.

    Example:
        If the CSV minimum is at 2000 and current_spot is 2400,
        all strikes are multiplied by 2400 / 2000 = 1.2.
    """
    shifted = target_profile.copy()
    shifted.index = shifted.index.astype(float)

    min_strike = float(shifted[payoff_col].idxmin())
    if min_strike <= 0:
        raise ValueError("Cannot homothetically shift target profile with non-positive minimum strike.")

    scale = float(current_spot) / min_strike
    shifted.index = shifted.index * scale
    shifted.index.name = target_profile.index.name

    return shifted


def smooth_target_profile(target_profile, payoff_col="Payoff($)", smooth_factor=1e13):
    strikes = target_profile.index.astype(float).to_numpy()
    payoffs = target_profile[payoff_col].astype(float).to_numpy()

    spline = UnivariateSpline(strikes, payoffs, s=smooth_factor)

    smoothed = target_profile.copy()
    smoothed[payoff_col] = spline(strikes)
    return smoothed


def load_target_profile():
    base_filename = "data/ETH - target shifted v2.csv"
    filename = base_filename
    if os.path.exists("../" + base_filename):
        filename = "../" + base_filename
    elif os.path.exists("../../" + base_filename):
        filename = "../../" + base_filename
    elif os.path.exists("../../../" + base_filename):
        filename = "../../../" + base_filename
    target_profile = pd.read_csv(filename, index_col=0)  # "Payoff ($)")
    smoothed_profile = smooth_target_profile(target_profile)
    return smoothed_profile

def build_parametric_target_profile(
    asset: str, spot_ladder: list[float] | np.ndarray, current_spot: float, **kwargs,
):
    if asset == "ETH":
        return build_parametric_target_profile_eth(spot_ladder, current_spot, **kwargs)
    elif asset == "FIL":
        return build_parametric_target_profile_fil(spot_ladder, current_spot, **kwargs)
    else:
        raise ValueError(
            f"Unsupported asset: {asset}. Supported assets are 'ETH' and 'FIL'."
        )

def _scaled_linear_v_target_profile(
    spot_ladder, current_spot, payoff_col,
    low_floor_ratio, high_plateau_ratio, trough_payoff,
):
    """Client-specified shape (2026-08-24 screenshot, "always scale to current
    spot and max loss"): a straight, UNBOUNDED line in raw price on each side of
    current_spot — no flattening plateau at all, unlike the old log-moneyness
    engine this replaces. trough_payoff ("max loss") sets the depth exactly at
    spot; low_floor_ratio/high_plateau_ratio place the $0-breakeven crossing at
    spot*(1 - low_floor_ratio) and spot*(1 + high_plateau_ratio) — same
    ratio-as-%-of-spot meaning the UI's down/up-% knobs already had, so slope is
    simply |max_loss| / (ratio * spot) on each side and stays fixed forever
    beyond breakeven instead of leveling off. Reverse-engineered from the ETH
    example (spot $2,200, max loss -$20M, breakeven $200/$4,200 -> ratio
    2000/2200=0.909 both sides) and the FIL example (spot $0.75, max loss
    -$15.75M matching its own downside slope, breakeven $0.00/$2.0625 ->
    ratio 1.0 down / 1.75 up)."""
    strikes = np.asarray(spot_ladder, dtype=float)
    spot = float(current_spot)
    max_loss = abs(float(trough_payoff))
    slope_down = max_loss / (float(low_floor_ratio) * spot)
    slope_up = max_loss / (float(high_plateau_ratio) * spot)
    payoffs = np.where(
        strikes <= spot,
        -max_loss + slope_down * (spot - strikes),
        -max_loss + slope_up * (strikes - spot),
    )

    # No smooth_target_profile() here, unlike the old log-moneyness engine: that
    # spline (fixed s=1e13) was tuned for a gentle curve and barely touched it,
    # but this shape is a much sharper straight-line V — the same tolerance
    # rounded the trough off by over $2M against the client's exact numbers.
    # A piecewise-linear target is already well-behaved for the LP fit, so
    # there's nothing to smooth for.
    return pd.DataFrame(
        {payoff_col: payoffs},
        index=pd.Index(strikes, name="Strike($)"),
    )

def build_parametric_target_profile_eth(
    spot_ladder: list[float] | np.ndarray,
    current_spot: float,
    payoff_col: str = "Payoff($)",
    low_floor_ratio: float = 10 / 11,  # $2,000 breakeven distance at the client's $2,200 example spot
    trough_ratio: float = 1.0,
    high_plateau_ratio: float = 10 / 11,
    low_floor_payoff: float = None,
    trough_payoff: float = -20_000_000.0,
    high_plateau_payoff: float = None,
) -> pd.DataFrame:
    """low_floor_ratio/high_plateau_ratio are breakeven distance as a fraction of
    spot (0.909091 = $2,000 away when spot is $2,200, the client's ETH example);
    trough_payoff is the "max loss" hit exactly at spot. low_floor_payoff/
    high_plateau_payoff/trough_ratio are accepted but unused — kept only so the
    existing API/UI plumbing (OptimizerRunParams, the /target-profile and /run
    request schemas, v2's Wing-recovery knobs) doesn't need to change; the new
    shape has no flat plateau for them to set the level of. See
    _scaled_linear_v_target_profile for the actual math."""
    return _scaled_linear_v_target_profile(
        spot_ladder, current_spot, payoff_col,
        low_floor_ratio, high_plateau_ratio, trough_payoff,
    )

def build_parametric_target_profile_fil(
    spot_ladder: list[float] | np.ndarray,
    current_spot: float,
    payoff_col: str = "Payoff($)",
    low_floor_ratio: float = 1.0,
    trough_ratio: float = 1.0,
    high_plateau_ratio: float = 1.75,
    low_floor_payoff: float = None,
    trough_payoff: float = -15_750_000.0,
    high_plateau_payoff: float = None,
) -> pd.DataFrame:
    """FIL version of build_parametric_target_profile_eth (see there for the
    parameter meanings and the "unused, kept for plumbing compat" note).
    Reverse-engineered from the client's FIL example (spot $0.75): downside
    breakeven exactly at $0 (ratio 1.0), upside breakeven at $2.0625 (ratio
    1.75), max loss -$15.75M — taken from the example's downside slope
    (-$21M per $1 of FIL), since the screenshot's own trough cell (-$21M)
    was inconsistent with its downside rows by exactly that amount."""
    return _scaled_linear_v_target_profile(
        spot_ladder, current_spot, payoff_col,
        low_floor_ratio, high_plateau_ratio, trough_payoff,
    )