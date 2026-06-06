# Optimization Research — HEMS/EMS Provider Comparison

> Research into how major home solar + battery energy management providers handle optimization:
> ML, forecast inputs, algorithm types, decision cycles, degradation modeling, and grid services.
> Purpose: identify gaps and opportunities for this project.
> Date: June 2026.

---

## Provider Summaries

### Fronius Solar.web / Energy Cost Assistant (ECA)

One of the most technically explicit consumer products. Ingests three forecast streams continuously: PV yield forecast (Open-Meteo or Solcast), dynamic electricity prices (Octopus Agile, Tibber, Nordpool), and a household consumption forecast built from historical usage patterns (ML-based). Combines these in a daily optimization described as an LP/heuristic hybrid with a 24-hour rolling horizon.

**Standout feature:** explicitly models battery degradation in the objective function — the optimizer will refuse cycles where the expected financial gain is lower than the amortized degradation cost. This is publicly documented and unique among consumer products. The system adapts consumption profiles over time. Decision cycle appears to be continuous/~hourly; intraday updates as measurements deviate.

---

### SolarEdge ONE (formerly StormGuard / Home Energy Management)

Generates a new 24-hour energy plan at midnight each day — classic once-daily MPC horizon with intraday corrections as measurements deviate. Uses three inputs: external (weather API, dynamic tariff), internal historical (ML-trained usage patterns by time-of-day and day-of-week), and homeowner preferences (backup reserve, load priorities).

**Standout feature:** StormGuard — weather-triggered pre-charge to backup reserve when severe weather is forecast. Also offers appliance-level load control, which is absent from most competitors. Algorithm type is described as "AI" without technical detail, consistent with LP or MILP running cloud-side. Intraday corrections are partial (midnight re-plan is the seed); update frequency unknown.

---

### Tibber Smart Battery / Tibber HEMS

Tibber operates as both energy retailer and optimizer, giving it control of both the price signal and the dispatch signal. Optimizes in **15-minute intervals**, aligned with Nordpool intraday settlement. Objective is explicit cost minimization: charge during negative/low-price windows, discharge during high-price windows. Algorithm is primarily price-signal-driven with greedy LP elements — no ML-based load forecasting documented.

**Standout feature:** Grid Rewards VPP — launched in Sweden (SE3) in December 2024. Tibber aggregates residential batteries for grid balancing, providing demand response to the TSO. Participating households earn revenue on top of arbitrage. This is the most operationally sophisticated feature of any provider for the Swedish market. The battery can be commanded by Tibber during Grid Rewards activation windows (Tibber has operational primacy). 

---

### EcoFlow PowerOcean + AI EMS (OASIS)

Launched CES 2025, European HEMS expansion Q3 2025. Claims 90% forecast accuracy (tied to Solcast integration) and up to 77.6% bill reduction. Confirmed Solcast customer: uses satellite-derived irradiance for both user-facing PV forecast display and as a dispatch engine input. Optimization horizon 24–48 hours; algorithm undisclosed but likely LP or scenario-based MPC given probabilistic Solcast input.

**Standout feature:** Solcast probabilistic forecast integration (P10/P50/P90 irradiance). Also partnered with Tibber, meaning EcoFlow handles PV/battery physics while Tibber handles price scheduling — a stacked optimization architecture. Load forecasting uses historical patterns (claimed, not detailed). Degradation modeling not documented.

---

### Sonnen / SonnenOS

Oldest residential battery platform (Shell-owned since 2019). Default optimization is rule-based self-consumption. More sophisticated optimization is tied to the **sonnenFlat** tariff product, which pools batteries into the **sonnenVPP**. In December 2024, the VPP received approval from a German grid operator for **primary frequency regulation (FCR)** — technically the tightest response window (30-second FCR) of any consumer product.

**Standout feature:** FCR grid services. Sonnen's value proposition is grid services depth rather than algorithmic sophistication at the single-household level. Household-level optimization for non-VPP users is basic rule-based self-consumption. No LP, no degradation modeling documented.

---

### Tesla Powerwall + Tesla Energy Plan

Three modes: Self-Powered, Time-Based Control (TOU), Backup-Only. PV forecast uses satellite weather data. Time-Based Control minimizes cost under fixed TOU utility rates — no real-time Nordpool/dynamic tariff integration. Behind Tibber and EcoFlow for dynamic European markets.

**Standout feature:** Storm Watch — monitors external weather services and auto-charges to 100% before severe weather events. Adaptive learning is documented but vague ("learns" daily production and consumption patterns via rolling historical averages). VPP participation exists in Australia (AGL partnership) but not Sweden. Degradation not in optimizer.

---

### Enphase IQ System Controller / Ensemble

Microinverter-per-panel architecture; optimization via Enlighten cloud platform. Modes: Self-consumption, TOU, Backup, Savings. Integrates dynamic tariffs where available. Storm Guard (equivalent to Tesla/SolarEdge storm pre-charge). **IQ Load Controller** provides appliance-level shedding during outages or battery depletion — demand-side management absent from most competitors.

---

### Home Assistant + EMHASS (open-source reference)

EMHASS (Energy Management for Home Assistant) uses **Linear Programming** (PuLP library, HiGHS solver) as its core. LP minimizes electricity cost over a configurable 24–48 hour horizon with decision variables for battery charge/discharge, deferrable load schedules, and grid import/export. Constraints include SoC min/max, charge/discharge rate limits, grid export limits, and minimum SoC targets. PV forecast from PVLib (physics model identical in approach to this project) or Solcast/Open-Meteo. Nordpool day-ahead prices via HA Nordpool integration.

Real-world EMHASS results: **5–8% daily economic gain** vs rule-based control. Decision cycle: user-configurable cron, typically 30–60 min. No degradation modeling in base EMHASS; ML-based load forecasting being added. Reference paper: *"Optimizing Smart Home Energy Management: A Linear Programming Approach with EMHASS and Home Assistant"* (ResearchGate, 2024).

---

### Victron Energy Venus OS

Prosumer/DIY platform. Built-in ESS has: Optimized with BatteryLife (self-consumption + adaptive SoC minimum), Keep Batteries Charged, Scheduled Charging (up to 5 TOU windows). No day-ahead price integration natively; users script via Node-RED or dbus.

**Standout feature:** BatteryLife — a learned adaptive SoC floor. Tracks daily solar energy and adjusts the minimum SoC to ensure the battery survives to the next solar window, preventing overnight depletion. Simple but effective learned heuristic that most commercial providers lack. Full open API (Venus OS on Linux, dbus interface) enables community-built Nordpool LP optimization via Node-RED.

---

### Loxone Miniserver Energy Management

Building automation controller (not inverter vendor). Spot Optimizer ingests day-ahead spot prices and applies a configurable price-differential threshold to identify charge windows. Rule-based with configurable price triggers — not LP or MPC. No AI load forecasting; uses preprogrammed schedules and occupancy sensors.

**Standout feature:** Multi-device coordination. Because the Miniserver controls HVAC, EV charging, appliances, and battery simultaneously, it can perform demand-side management at a level most single-vendor HEMS cannot — pre-heat thermal mass, defer EV charging, and manage battery discharge as an integrated optimization, even if each individual control loop is rule-based.

---

## Feature Comparison Matrix

| Feature | Fronius ECA | SolarEdge ONE | Tibber | EcoFlow | Sonnen | Tesla PW | Enphase | EMHASS (HA) | Victron | Loxone | **This project** |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **Algorithm** | LP+heuristic | ML+LP | Rule/greedy | LP/MPC (likely) | Rule | Rule | Rule | **LP (PuLP)** | Rule | Rule | Rule (TOU) |
| **PV forecast** | Open-Meteo/Solcast | Weather API | Solcast | Solcast | ✗ | Satellite | Weather | PVLib/Solcast | ✗ | User | GTI+physics+ML |
| **Learned PV corrections** | Partial | Partial | ✗ | ✗ | ✗ | Partial | ✗ | ✗ | ✗ | ✗ | ✅ (90-day ratio) |
| **Load forecasting** | ✅ (ML patterns) | ✅ (ML patterns) | ✗ | ✅ (historical) | ✗ | ✅ (rolling avg) | ✅ (historical) | ✅ (avg, ML coming) | ✗ | ✅ (schedule+occ.) | ✗ |
| **Day-ahead prices** | ✅ | ✅ | ✅ | ✅ | ✗ | ✗ | Partial | ✅ (Nordpool) | User | ✅ (spot) | ✅ (Nordpool) |
| **Intraday prices** | ✅ (continuous) | Partial | ✅ (15-min) | ✅ (via Tibber) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Decision cycle** | ~hourly | 24h+corrections | 15 min | ~hourly | Event | Event | Event | 30–60 min cron | Event | Event | Daily (manual TOU) |
| **Battery degradation** | ✅ (documented) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Storm Watch** | ✅ | ✅ | ✗ | ✗ | ✗ | ✅ | ✅ | ✗ | ✗ | ✗ | ✗ |
| **VPP / Grid services** | ✗ | ✗ | ✅ (SE3 active) | ✅ (via Tibber) | ✅ (FCR, DE) | US only | US only | ✗ | ✗ | ✗ | ✗ |
| **Adaptive SoC floor** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✅ (BatteryLife) | ✗ | ✗ |
| **Probabilistic forecast** | ✗ | ✗ | ✗ | ✅ (Solcast P10/P90) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **48h+ horizon** | ✅ | ✅ | 24h | 48h | ✗ | ~24h | ~24h | User-defined | ✗ | 24h | 24h (today) |
| **Deferrable load scheduling** | ✗ | ✅ | ✗ | ✗ | Limited | ✗ | ✅ | ✅ | ✗ | ✅ | ✗ |
| **Multi-day pre-charge** | ✅ | ✅ | ✗ | ✗ | ✗ | ✅ (Storm Watch) | ✅ | ✗ | ✗ | ✗ | ✗ |
| **Open API** | Limited | Limited | ✅ (public) | Partial | Limited | Very limited | Limited | ✅ (open-source) | ✅ (full) | Limited | ✅ (self-built) |

---

## Gap Analysis — What 2+ Providers Have That This Project Lacks

Ranked by provider count and practical fit for SE3 / this installation:

---

### Gap 1 — Load Forecasting
**Providers:** Fronius, SolarEdge, EcoFlow, Tesla, Enphase, EMHASS (7/10)

Currently the project has no load forecast. The battery simulation in `buildIntradayForecast` uses yesterday's actual load profile as a proxy — which is reasonable but doesn't account for day-of-week patterns (weekday vs. weekend) or seasonal variation.

**Minimum viable approach:** Per-slot (5-min) average `load_kw` by day-of-week and month, computed from `energy_readings` over 30+ days. This is a pure Supabase query, no ML needed. Academic benchmarks (arxiv:2501.05000) show this simple approach often matches LSTM-level accuracy for single-household load forecasting.

**Enhanced approach:** Exponential smoothing or gradient boosting with features: time-of-day, day-of-week, month, temperature. Outperforms naive average by ~15–30% RMSE.

---

### Gap 2 — LP/MILP Optimizer Replacing TOU Heuristic
**Providers:** Fronius, SolarEdge, EcoFlow, EMHASS — academic standard (5+)

The current TOU suggestion uses a heuristic: identify cheap hours for grid-charging, expensive hours for discharging, generate a schedule. This is suboptimal when the price curve has multiple cheap/expensive windows, partial solar, and a bounded battery.

A **single LP per day over 288 5-min slots** (or 24 hourly aggregated slots) with:
- Decision variables: `charge_kw[t]`, `discharge_kw[t]`, `import_kw[t]`, `export_kw[t]`, `soc[t]`
- Objective: minimize `sum(import_kw[t] * price[t]) - sum(export_kw[t] * price[t])`
- Constraints: SoC dynamics, SoC min/max, charge/discharge rate limits, binary exclusion (can't charge and discharge simultaneously — requires MILP)

EMHASS ships this in Python/PuLP and is the best open-source reference. MDPI Batteries 2024 shows MILP achieves higher profit than LP alone due to the simultaneous charge/discharge exclusion constraint. Real-world EMHASS results: **5–8% daily gain** vs. rule-based control.

---

### Gap 3 — Battery Degradation Cost in the Optimizer
**Providers:** Fronius (the only commercial product to document this)
**Academic standard:** Well-covered; linear convex approximation is LP-compatible

Fronius is the only consumer product that publicly penalizes uneconomic cycles. For LiFePO4 (this installation's chemistry), degradation cost per cycle is modest but non-zero. The framework prevents the optimizer from performing high-frequency shallow cycling that appears profitable but accumulates calendar/cycle damage.

**Approach:** Add degradation cost term `λ × DoD` to the LP objective.
```
min: Σ(import_cost[t]) - Σ(export_earn[t]) + λ × Σ(charge_kw[t] + discharge_kw[t]) × Δt
```
Where `λ` = battery_cost_SEK / warranty_cycles / battery_kwh — a scalar that converts kWh cycled to SEK degradation cost. For LiFePO4 at ~6000 cycle warranty and 80,000 SEK battery cost: λ ≈ 80,000 / 6,000 / 20 = 0.67 SEK/kWh cycled. This means arbitrage must exceed ~0.67 SEK/kWh margin to be worth cycling.

Reference: Xu et al. (2018) linear convex approximation; Wiley Energy Storage (2024) quantification.

---

### Gap 4 — Storm Watch / Multi-Day Pre-Charge Rule
**Providers:** Fronius, SolarEdge, Tesla, Enphase (4/10)

If the 5-day solar forecast predicts a multi-day low-irradiance period, the optimal strategy shifts from arbitrage to maximizing stored energy ahead of the low-generation window. None of the commercial providers describe anything more sophisticated than a conditional override: if `sum(forecast_pv, next_48h) < threshold`, raise minimum SoC to 80% (or charge to 100% on the last cheap window before the event).

The current project already has a 5-day solar forecast on the Trender tab. Extending the TOU suggestion to include this pre-charge logic would require:
- Reading the 5-day kWh forecast
- If `D+1 + D+2 < 20 kWh` (e.g., < 35% of typical), flag "low solar window ahead"
- Inject a "charge to 90%" window in the TOU schedule for tonight's cheapest hours

---

### Gap 5 — Intraday / Continuous Re-Optimization
**Providers:** Tibber (15-min), Fronius (continuous), EcoFlow (via Tibber) (3/10)

The current project dispatches a TOU schedule once per day (built at 22:10 UTC by the `build_suggest` cron). Tibber re-solves every 15 minutes as intraday prices and real SoC deviate from the plan. In SE3, the intraday market (ELBAS) can show meaningful deviations from day-ahead — particularly in hours adjacent to major wind ramp events.

**Minimal implementation:** re-run the TOU suggestion cron at 06:00 UTC (after today's prices are confirmed) in addition to the existing 22:10 UTC run. Full intraday re-optimization (every 15 min using real SoC) would require significant architectural change.

---

### Gap 6 — Probabilistic Forecast / Uncertainty-Aware Dispatch
**Providers:** EcoFlow (Solcast P10/P50/P90) — 1 commercial; well-covered academically

The current project's learned per-slot ratio corrections reduce systematic bias but do not model forecast uncertainty. On a borderline day (partly cloudy, cloud cover ~50%), the P50 kWh estimate may be correct but the P10–P90 range is wide — the dispatch decision under uncertainty should be different than on a clear day.

**Approaches:**
1. **Compute empirical prediction intervals** from 90-day correction residuals per slot (low effort, uses existing data).
2. **Integrate Solcast** free tier (10 API calls/day, sufficient for one daily fetch) to get P10/P50/P90 from a separate ML-based model.
3. **Risk-adjusted dispatch:** when PV forecast uncertainty is high (wide P10–P90 band), hold extra SoC reserve rather than committing to aggressive discharge.

Reference: *"Ensemble Nonlinear MPC for Residential Solar-Battery Energy Management"* (Yang Li et al., IEEE Trans. Control Sys. Tech., 2023. arxiv:2303.10393).

---

### Gap 7 — 48-Hour Optimization Horizon
**Providers:** EcoFlow (48h), Fronius, SolarEdge (24h+), academic literature recommends 48–72h (4+)

The current project optimizes a single day in isolation. In Sweden, multi-day weather patterns (persistent anticyclonic/frontal systems) mean tomorrow's solar yield strongly predicts whether tonight's battery charge is needed. A 48-hour LP that carries SoC across the day boundary can outperform two independent 24-hour LPs.

**Implementation:** Extend the LP to 576 slots (48h) using today's prices (known) and tomorrow's prices (available from Nordpool from ~13:00 CET). Already partly in the project: `buildTomorrowForecast` projects midnight SoC, so the day-boundary chain is partly modeled — it just isn't connected to the dispatch optimizer.

---

### Gap 8 — Adaptive SoC Floor (BatteryLife-style)
**Providers:** Victron Venus OS (BatteryLife) — the only commercial implementation documented

Victron's BatteryLife algorithm adjusts the minimum SoC floor dynamically based on how much solar the system expects tomorrow. If yesterday had poor solar and the battery was depleted, the floor is raised to protect against the same situation. Simple but highly effective at preventing the "drained battery on the first cloudy morning" failure mode.

The current project has a fixed 10% SoC floor. An adaptive version would raise it to 30–50% when the 5-day forecast shows consecutive low-solar days.

---

### Gap 9 — VPP / Grid Services (Tibber Grid Rewards — active in SE3)
**Providers:** Tibber (SE3 active Dec 2024), Sonnen (FCR, DE), EcoFlow via Tibber (2/10 for SE3)

Tibber's Grid Rewards is already live in Sweden SE3. It allows a Tibber-connected battery to be commanded to charge/discharge by Tibber's grid balancing layer, earning revenue beyond spot arbitrage. Sonnen's VPP received FCR approval in Germany in December 2024.

**This is less an optimization algorithm gap and more a commercial/integration decision.** Participating requires switching to Tibber as energy supplier and using a Tibber-compatible inverter (Growatt is not on the current compatibility list, but the list is expanding). Revenue potential: Tibber claims up to €740/year in combined arbitrage + grid rewards for a typical system.

---

## Academic Papers — Directly Applicable

| Paper | Key Contribution | Relevance |
|---|---|---|
| Yang Li et al. (IEEE 2023) — arxiv:2303.10393 | Ensemble nonlinear MPC for residential solar-battery under forecast uncertainty | LP → MPC with probabilistic PV; Gap 6 |
| MDPI Batteries 10(7) 2024 | Benchmarks LP vs MILP for residential arbitrage; MILP superior due to charge/discharge exclusion | Gap 2 reference implementation |
| MDPI Batteries 9(6) 2023 | MPC profitability analysis for residential battery storage | LP/MPC decision support |
| arxiv:2205.07700 | Compares MPC and SDDP; SDDP wins on multi-day horizon | Gap 7 (48h horizon) |
| Xu et al. (2018) — ResearchGate | LP-compatible convex degradation cost approximation | Gap 3 (degradation term) |
| Wiley Energy Storage (2024) doi:10.1002/est2.588 | Quantifies degradation cost impact on dispatch economics for LiFePO4 | Gap 3 validation |
| arxiv:2501.05000 | Benchmarks LSTM/CNN vs simple baselines for household load forecasting; simple baselines often competitive | Gap 1 (load forecasting) |
| MDPI Energies 2025 doi:10.3390/en18195262 | Systematic review of HEMS optimization 2019–2024 | Full literature survey |

---

## Open-Source Reference Implementations

| Project | What it provides | Link |
|---|---|---|
| **EMHASS** | Python LP (PuLP/HiGHS), PVLib, Nordpool, deferrable loads, HA integration | github.com/davidusb-geek/emhass |
| **EMHASS LP model** | Full LP math model documentation | emhass.readthedocs.io/en/latest/advanced_math_model.html |
| **nordpool-predict-fi** | XGBoost Nordpool price prediction using weather features | github.com/vividfog/nordpool-predict-fi |
| **WattWise** | AppDaemon + Tibber + solar, 15-min rolling LP dispatch | HA Community |
| **battery_energy_trading** | Nordpool integration, simple rule-based arbitrage | github.com/Tsopic/battery_energy_trading |
| **Solcast API** | P10/P50/P90 irradiance, 30-min resolution, 7-day, free hobbyist tier | docs.solcast.com.au |

---

## Summary — Prioritised Opportunity List

| # | Gap | Effort | Providers with it | Expected value |
|---|---|---|---|---|
| 1 | **Load forecasting** (per-slot historical average by day-of-week) | Low | 7/10 | Enables proper LP dispatch; removes biggest forecast blind spot |
| 2 | **LP/MILP optimizer** replacing TOU heuristic | Medium | 5+ | 5–15% cost reduction on complex price days |
| 3 | **Multi-day pre-charge rule** (storm watch) | Low | 4/10 | Prevents battery depletion during low-solar windows; already have forecast data |
| 4 | **Adaptive SoC floor** (raise before low-solar forecast) | Low | 1/10 (Victron) | Eliminates overnight depletion risk |
| 5 | **48-hour optimization horizon** | Medium | 4+ | Better multi-day weather + price handling |
| 6 | **Battery degradation cost in LP** | Medium | 1/10 + academic | Protects capex; prevents uneconomic micro-cycling |
| 7 | **Probabilistic PV forecast intervals** | Medium | 1/10 + academic | Risk-aware dispatch; reduces unexpected shortfalls |
| 8 | **Intraday re-solve** (at price-publication + 15-min rolling) | Medium | 3/10 | Captures intraday price movements |
| 9 | **VPP / Grid Rewards** (Tibber, active in SE3) | High (supplier change) | 2/10 (SE3) | Incremental revenue stacking on top of arbitrage |
| 10 | **Deferrable load scheduling** (EV, heat pump) | High (needs hardware) | 4/10 | Can rival battery arbitrage value |
