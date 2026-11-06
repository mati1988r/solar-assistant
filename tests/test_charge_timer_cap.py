"""Chg timer cap%: end of charge window + mathematical half-up rounding."""

from __future__ import annotations

from src.timer_plan import (
    ACTION_CHARGE_GRID,
    ACTION_DISCHARGE_GRID,
    _charge_timer_cap_pct,
    _round_pct_half_up,
    build_hour_timer_schedule,
    parse_timer_schedule_segments,
)


def _cfg(**sim_extra) -> dict:
    cfg = {
        "battery": {
            "capacity_kwh": 48.0,
            "max_charge_power_kw": 6.0,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {"min_soc_pct": 16, **sim_extra},
        "inverter": {"ac_capacity_kw": 8.0},
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 0.5},
    }
    return cfg


def _q(
    soc: float,
    *,
    battery_delta: float = 0.0,
    grid_import: float = 0.0,
    action: str = ACTION_CHARGE_GRID,
    reserve_kwh: float | None = None,
) -> dict:
    slot = {
        "action": action,
        "battery_delta": battery_delta,
        "grid_import": grid_import,
        "grid_export": 0.0,
        "pv": 0.0,
        "load": 0.1925,
        "soc_pct": soc,
    }
    if reserve_kwh is not None:
        slot["reserve_kwh"] = reserve_kwh
    return slot


def _cap_from_timer(txt: str) -> int:
    segs = parse_timer_schedule_segments(txt)
    assert segs and segs[0]["kind"] == "chg", txt
    return int(segs[0]["capacity_pct"])


# --- rounding ---


def test_round_pct_half_up_math_not_bankers():
    assert _round_pct_half_up(23.4) == 23
    assert _round_pct_half_up(23.5) == 24
    assert _round_pct_half_up(23.6) == 24
    # Python round(24.5) == 24 (bankers); math half-up → 25
    assert round(24.5) == 24
    assert _round_pct_half_up(24.5) == 25
    assert _round_pct_half_up(32.0) == 32


# --- _charge_timer_cap_pct unit ---


def test_cap_pct_is_this_hours_charge_end_not_back_to():
    """back-to is the run target. This hour's stop is the SOC its own window reaches."""
    slots = [_q(21.0, battery_delta=1.0, grid_import=1.2)]
    cfg = _cfg()
    cfg["inverter"]["back_to_battery_capacity"] = 26
    assert _charge_timer_cap_pct(slots, cfg) == 21


def test_cap_pct_uses_last_charge_quarter_not_hour_end():
    """Live H01: charge to 24.5% by 01:30, then drain to 23.6% — cap is 25%."""
    slots = [
        _q(21.8, battery_delta=1.2927, grid_import=1.6225, reserve_kwh=15.36),
        _q(24.5, battery_delta=1.2927, grid_import=1.6225, reserve_kwh=15.0),
        _q(24.1, battery_delta=-0.2081, action="Discharging to Load", reserve_kwh=14.5),
        _q(23.6, battery_delta=-0.2081, action="Discharging to Load", reserve_kwh=14.0),
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 25


def test_cap_pct_ignores_falling_overnight_reserve():
    """High early-night reserve must not inflate / invert the stop %."""
    # Reserve ≈ 32% of 48kWh, but charge window ends at 25.0%
    slots = [
        _q(19.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=15.36),
        _q(22.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=15.0),
        _q(24.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.5),
        _q(25.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.0),
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 25


def test_cap_pct_math_rounds_charge_end_soc():
    slots = [
        _q(20.0, battery_delta=1.0, grid_import=1.2),
        _q(22.4, battery_delta=1.0, grid_import=1.2),  # 22.4 → 22
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 22
    slots[-1]["soc_pct"] = 22.5
    assert _charge_timer_cap_pct(slots, _cfg()) == 23


def test_cap_pct_floored_at_min_soc():
    slots = [
        _q(10.0, battery_delta=0.5, grid_import=0.6),
        _q(12.2, battery_delta=0.5, grid_import=0.6),
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 16


def test_cap_pct_empty_slots_returns_min_soc():
    assert _charge_timer_cap_pct([], _cfg()) == 16


def test_cap_pct_fallback_last_slot_when_no_charge_energy():
    """Action says Charging from Grid but deltas are zero — still use last chg SOC."""
    slots = [
        _q(18.0, action=ACTION_CHARGE_GRID),
        _q(21.6, action=ACTION_CHARGE_GRID),
    ]
    # End 21.6 → 22. The stop is that SOC, not a headroom above the window start.
    assert _charge_timer_cap_pct(slots, _cfg()) == 22


def test_cap_pct_is_charge_end_not_start_plus_five():
    """Stop is the SOC this window reaches. A later write may add SRNE headroom."""
    slots = [
        _q(17.4, battery_delta=0.33, grid_import=0.4),
        _q(18.1, battery_delta=0.33, grid_import=0.4),
    ]
    assert _charge_timer_cap_pct(slots, _cfg()) == 18


# --- build_hour_timer_schedule integration ---


def test_charge_kw_and_window_cover_the_cap():
    """Cap is this hour's charge-end SOC. Power covers that rise plus the house load."""
    cfg = _cfg()
    cfg["battery"]["capacity_kwh"] = 47.6
    cfg["battery"]["max_charge_power_kw"] = 5.8
    slots = [
        _q(19.4, battery_delta=0.65, grid_import=0.8, action=ACTION_CHARGE_GRID),
        _q(21.0, battery_delta=0.65, grid_import=0.8, action=ACTION_CHARGE_GRID),
        _q(21.0, battery_delta=0.0, grid_import=0.25, action="Idle - Grid Usage for Load"),
        _q(21.0, battery_delta=0.0, grid_import=0.25, action="Idle - Grid Usage for Load"),
    ]
    slots[0]["load"] = 0.15
    slots[1]["load"] = 0.15
    txt = build_hour_timer_schedule(
        5, slots, cfg, action=ACTION_CHARGE_GRID, bat_charge=1.3,
    )
    assert txt == "Chg 05:00-05:30 3.5kW cap21%", txt
    segs = parse_timer_schedule_segments(txt)
    assert segs[0]["to"] == "05:30"


def test_short_charge_at_hour_start_stays_against_previous_hour():
    """1 kWh in the first quarter packs to 30 min from :00, not the end of the hour."""
    cfg = _cfg()
    cfg["battery"]["capacity_kwh"] = 47.6
    cfg["battery"]["max_charge_power_kw"] = 5.8
    cfg["timer_schedule"]["min_hourly_transfer_kwh"] = 1
    cfg["inverter"]["back_to_battery_capacity"] = 26
    slots = [
        _q(18.0, battery_delta=0.97, grid_import=1.2),
        _q(18.0, battery_delta=0.0, grid_import=0.15, action="Idle - Grid Usage for Load"),
        _q(18.0, battery_delta=0.0, grid_import=0.15, action="Idle - Grid Usage for Load"),
        _q(18.0, battery_delta=0.0, grid_import=0.15, action="Idle - Grid Usage for Load"),
    ]
    txt = build_hour_timer_schedule(
        5, slots, cfg, action=ACTION_CHARGE_GRID, bat_charge=0.97,
    )
    assert txt == "Chg 05:00-05:30 3.0kW cap18%", txt


def test_charge_touching_both_hour_edges_is_not_split():
    """A charge hour that runs :00 to :60 stays one window."""
    cfg = _cfg()
    cfg["battery"]["capacity_kwh"] = 47.6
    cfg["battery"]["max_charge_power_kw"] = 5.8
    cfg["timer_schedule"]["min_hourly_transfer_kwh"] = 1
    cfg["inverter"]["back_to_battery_capacity"] = 26
    slots = [
        _q(16.5, battery_delta=0.25, grid_import=0.3),
        _q(17.0, battery_delta=0.25, grid_import=0.3),
        _q(17.5, battery_delta=0.25, grid_import=0.3),
        _q(18.0, battery_delta=0.25, grid_import=0.3),
    ]
    txt = build_hour_timer_schedule(
        5, slots, cfg, action=ACTION_CHARGE_GRID, bat_charge=1.0,
    )
    assert txt.startswith("Chg 05:00-06:00 "), txt


def test_timer_schedule_cap_matches_charge_window_physics():
    """Regression: cap24% from hour-end SOC would under-charge vs plan (→ ~23.1%)."""
    slots = [
        _q(21.8, battery_delta=1.2927, grid_import=1.6225, reserve_kwh=15.36),
        _q(24.5, battery_delta=1.2927, grid_import=1.6225, reserve_kwh=15.0),
        _q(24.1, battery_delta=-0.2081, action="Discharging to Load", reserve_kwh=14.5),
        _q(23.6, battery_delta=-0.2081, action="Discharging to Load", reserve_kwh=14.0),
    ]
    txt = build_hour_timer_schedule(
        1, slots, _cfg(), action=ACTION_CHARGE_GRID, bat_charge=2.585,
    )
    assert "Chg 01:00-01:30" in txt, txt
    assert _cap_from_timer(txt) == 25
    # Hour-end SOC 23.6 must NOT drive the cap
    assert _cap_from_timer(txt) != 24


def test_consecutive_hours_caps_track_each_hour_charge_end():
    """Multi-hour Chg: each hour has its own cap from that hour's charge end."""
    cfg = _cfg()
    h1_slots = [
        _q(19.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=15.36),
        _q(22.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=15.0),
        _q(24.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.5),
        _q(25.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.0),
    ]
    h2_slots = [
        _q(27.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=14.0),
        _q(29.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=13.8),
        _q(31.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=13.5),
        _q(32.0, battery_delta=1.2, grid_import=1.5, reserve_kwh=13.2),
    ]
    # Falling reserve would have yielded ~32% then ~29% — must not happen
    assert _charge_timer_cap_pct(h1_slots, cfg) == 25
    assert _charge_timer_cap_pct(h2_slots, cfg) == 32
    assert _cap_from_timer(
        build_hour_timer_schedule(1, h1_slots, cfg, action=ACTION_CHARGE_GRID, bat_charge=4.8)
    ) == 25
    assert _cap_from_timer(
        build_hour_timer_schedule(2, h2_slots, cfg, action=ACTION_CHARGE_GRID, bat_charge=3.6)
    ) == 32


def test_full_hour_charge_cap_is_end_soc():
    slots = [
        _q(20.1, battery_delta=1.0, grid_import=1.3),
        _q(22.3, battery_delta=1.0, grid_import=1.3),
        _q(24.7, battery_delta=1.0, grid_import=1.3),
        _q(27.2, battery_delta=1.0, grid_import=1.3),
    ]
    txt = build_hour_timer_schedule(
        3, slots, _cfg(), action=ACTION_CHARGE_GRID, bat_charge=4.0,
    )
    assert "Chg 03:00-04:00" in txt, txt
    assert _cap_from_timer(txt) == 27  # 27.2 → 27


# 2026-11-06 Energy arbitrage forecast, hourly kWh. PV stays at 0 until 08:00.
_NOV6_PV = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    0.685, 1.582, 2.237, 2.306, 2.377, 2.596, 1.999, 0.657,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
]
_NOV6_LOAD = [
    0.788, 1.065, 0.883, 0.548, 0.599, 0.694, 0.577, 0.52,
    0.592, 0.602, 0.53, 0.842, 0.779, 0.609, 0.881, 0.852,
    0.756, 0.959, 0.941, 0.991, 0.977, 0.806, 0.86, 1.218,
]
_NOV7_PV = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    0.837, 1.531, 2.622, 3.226, 2.461, 1.573, 0.999, 0.281,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
]
_NOV7_LOAD = [
    0.856, 0.828, 0.678, 0.579, 0.555, 0.557, 0.61, 0.553,
    0.62, 0.559, 0.624, 0.948, 1.055, 1.122, 0.868, 0.754,
    1.114, 1.388, 1.206, 1.081, 1.269, 1.125, 0.998, 1.207,
]


def test_nov6_morning_charge_window_sits_above_back_to():
    """Full day from 00:00. Inverter pair is to-grid 16 / back-to 26.

    Each charge hour's cap is the SOC that hour's window reaches.
    The run ends above back-to. Every filled step buys its house load.
    The last hour runs at the charge-power limit.
    """
    from datetime import datetime

    from src.g12_pricing import get_buy_price
    from src.grid_config import merge_grid_defaults
    from src.plan_optimizer import optimize_horizon
    from src.plan_physics import simulate_hour
    from src.simulation_config import (
        get_simulation_params,
        merge_battery_defaults,
        merge_simulation_defaults,
    )

    capacity = 47.6
    cfg = {
        "battery": {
            "capacity_kwh": capacity,
            "max_charge_power_kw": 5.8,
            "max_discharge_power_kw": 8.0,
        },
        "simulation": {
            "min_soc_pct": 18,
            "epsilon_kwh": 0.05,
            "losses_pct": {
                "grid_to_battery": 7.5,
                "battery_to_load_or_grid": 7.5,
                "pv_to_grid": 7.5,
                "pv_to_load": 7.5,
                "pv_to_battery": 7.5,
            },
        },
        "inverter": {
            "ac_capacity_kw": 8.0,
            "to_grid_battery_capacity": 16,
            "back_to_battery_capacity": 26,
        },
        "grid": {
            "feed_in_price_pln": 0.2,
            "grid_export_threshold_pln_kwh": 0.68,
            "g12": {
                "tariff_preset": "G12",
                "tariff_name": "Energa G12",
                "peak_price_pln_kwh": 1.2444,
                "offpeak_price_pln_kwh": 0.6229,
                "peak_hours_weekday": [[6, 13], [15, 22]],
            },
        },
        "timer_schedule": {"min_block_minutes": 30, "min_hourly_transfer_kwh": 1},
    }
    merge_grid_defaults(cfg)
    merge_simulation_defaults(cfg)
    merge_battery_defaults(cfg)
    params = get_simulation_params(cfg)
    day = datetime(2026, 11, 6)
    day2 = datetime(2026, 11, 7)
    scale = 0.25
    pv, load, buy = [], [], []
    for date, pv_day, load_day in (
        (day, _NOV6_PV, _NOV6_LOAD),
        (day2, _NOV7_PV, _NOV7_LOAD),
    ):
        for hour in range(24):
            price, _zone = get_buy_price(date.replace(hour=hour), cfg)
            for _quarter in range(4):
                pv.append(pv_day[hour] * scale)
                load.append(load_day[hour] * scale)
                buy.append(price)
    controls = optimize_horizon(
        steps=192,
        pv_series=pv,
        load_series=load,
        buy_prices=buy,
        rce_series=[0.4] * 96,
        initial_soc_kwh=capacity * 0.16,
        cfg=cfg,
        params=params,
        end_dt=day2.replace(hour=23),
        today_date=day.date(),
        rce_map={},
        forecast={
            "today": {"pv": _NOV6_PV, "load": _NOV6_LOAD},
            "tomorrow": {"pv": _NOV7_PV, "load": _NOV7_LOAD},
        },
        step_scale=scale,
        rce_step_offset=0,
        front_load_skip_leading_slots=4,
    )

    soc = capacity * 0.16
    min_kwh = params["min_soc_pct"] / 100.0 * capacity
    by_hour: dict[int, list[dict]] = {}
    for i, control in enumerate(controls):
        phys = simulate_hour(
            soc,
            pv[i],
            load[i],
            control,
            battery_cap=capacity,
            min_kwh=min_kwh,
            ac_cap_kw=8 * scale,
            eta_grid=params["eta_grid_battery"],
            eta_out=params["eta_battery_out"],
            eta_pv_load=params["eta_pv_load"],
            eta_pv_grid=params["eta_pv_grid"],
            eta_pv_battery=params["eta_pv_battery"],
            epsilon=0.05 * scale,
            discharge_dc_cap_kwh=8 * scale,
        )
        if control.grid_charge_kw > 0.01:
            action = ACTION_CHARGE_GRID
        elif phys.battery_delta < -0.001:
            action = "Discharging to Load"
        else:
            action = "Idle - Grid Usage for Load"
        by_hour.setdefault(i // 4, []).append({
            "soc_pct": round(phys.soc_end / capacity * 100, 2),
            "battery_delta": phys.battery_delta,
            "grid_import": phys.grid_import,
            "grid_export": phys.grid_export,
            "pv": pv[i],
            "load": load[i],
            "action": action,
        })
        soc = phys.soc_end

    def _charge_hours(day_index: int) -> list[int]:
        base = day_index * 24
        return [
            hour - base
            for hour in range(base, base + 24)
            if any(s["action"] == ACTION_CHARGE_GRID for s in by_hour.get(hour, []))
        ]

    lines: list[str] = []
    for abs_hour in range(48):
        slots_h = by_hour[abs_hour]
        date = day if abs_hour < 24 else day2
        clock = abs_hour % 24
        charging = any(s["action"] == ACTION_CHARGE_GRID for s in slots_h)
        bat = sum(max(0.0, s["battery_delta"]) for s in slots_h)
        export = sum(s["grid_export"] for s in slots_h)
        timer = ""
        if charging and bat > 0.001:
            timer = build_hour_timer_schedule(
                clock, slots_h, cfg, action=ACTION_CHARGE_GRID, bat_charge=bat,
            )
        elif export > 0.001:
            timer = build_hour_timer_schedule(
                clock, slots_h, cfg, action=ACTION_DISCHARGE_GRID, grid_export=export,
            )
        soc_end = slots_h[-1]["soc_pct"]
        lines.append(
            f"{date.date()} {clock:02d}:00  soc {soc_end:5.2f}%  {timer or slots_h[-1]['action']}"
        )
    # Charge before 06:00 is one block against the end of that cheap window.
    assert _charge_hours(0) == [4, 5], lines
    nov7_morning = [h for h in _charge_hours(1) if h < 6]
    assert nov7_morning == list(range(nov7_morning[0], 6)), lines
    assert not any(s["action"] == ACTION_CHARGE_GRID for s in by_hour[6])
    assert not any(s["action"] == ACTION_CHARGE_GRID for s in by_hour[13])
    assert not any(s["action"] == ACTION_CHARGE_GRID for s in by_hour[14])

    # Hour 4 only takes the tail that does not fit in hour 5 at 5.8 kW.
    hour4_kwh = sum(max(0.0, s["battery_delta"]) for s in by_hour[4])
    assert hour4_kwh < 1.5, hour4_kwh
    hour4_txt = build_hour_timer_schedule(
        4,
        by_hour[4],
        cfg,
        action=ACTION_CHARGE_GRID,
        bat_charge=hour4_kwh,
    )
    assert hour4_txt == "Chg 04:30-05:00 3.0kW cap18%", hour4_txt

    slots = by_hour[5]
    txt = build_hour_timer_schedule(
        5,
        slots,
        cfg,
        action=ACTION_CHARGE_GRID,
        bat_charge=sum(max(0.0, s["battery_delta"]) for s in slots),
    )
    assert txt == "Chg 05:00-06:00 5.8kW cap29%", txt
    seg = parse_timer_schedule_segments(txt)[0]
    assert seg["capacity_pct"] == 29
    assert seg["power_kw"] == cfg["battery"]["max_charge_power_kw"]
    assert slots[-1]["soc_pct"] == 28.99
