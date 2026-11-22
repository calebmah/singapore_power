# <img src="custom_components/singapore_power/brand/icon.png" width="40" height="40" align="top" alt="Singapore Power" /> Singapore Power (SP Group) Home Assistant Integration

Custom Home Assistant integration (`v0.3.6`) for **Singapore Power (SP Group)** utilities and smart energy services in Singapore — covering **Multi-Premise Electricity, Water, Piped Gas, Tengah Centralized Cooling & Room FCUs, Billing & Payments, Hardware Meter Readings, Regulated Electricity / Gas / Water Tariffs & OEM Comparisons, EV Charging & Rewards, Sustainability Green Goals, and GreenUP Loyalty**.

> [!NOTE]
> **AI & Unofficial Project Disclaimer:** Portions of this integration (including API analysis, code, and documentation) were developed with the assistance of AI tools and tested against a live Home Assistant instance. This is an independent community project and is not affiliated with, endorsed by, or supported by SP Group. Use at your own risk.

## Key Capabilities

- **Full Multi-Premise Household Utility Coverage:**
  - **Electricity (`kWh` & `SGD`):** Monotonic cumulative total (Energy Dashboard ready), Today's usage & cost, Yesterday's usage & cost (with half-hourly slot breakdown), Latest published 30-minute smart meter slot (automatically trimming padded future/unreported `0.0` slots), Month-to-Date usage & cost, Last Billed Month usage, Neighbour Average, Efficient Neighbour (Top 25%) Benchmark, 12-Month Average, Actual Hardware Meter Reading, and Official SP Regulated Tariff + Open Electricity Market (OEM) best fixed/discount plan attributes.
  - **Custom OEM Tariff / Automatic Rate Fallback:** When SP's residential AMI feed omits per-slot `amount` (`null`), electricity cost sensors and Recorder cost statistics automatically compute SGD cost from your configured OEM rate (`electricity_price` option) or SP's live regulated tariff — and dynamically reprice historical Recorder cost statistics when you change your rate.
  - **Water (`m³` & `SGD`):** Monotonic cumulative total (Energy Dashboard ready), Smart Water Meter (AMI) Today / Yesterday / Latest 30m Slot / Month-to-Date usage & SGD cost, Last Billed Month usage, Neighbour Average, Efficient Neighbour Benchmark, 12-Month Average, Actual Hardware Meter Reading, and **Official PUB All-In Domestic Water Price** (`0–40 m³` & `> 40 m³` tiers incl. Water Tariff + Water Conservation Tax + Waterborne Tax + GST via SingStat `M890981`).
  - **Piped Town Gas (`kWh` or `m³` & `SGD`):** Monotonic cumulative total, AMI Today / Month-to-Date usage & SGD cost (where smart gas meters are installed), Last Billed Month usage, Neighbour Average, Efficient Neighbour Benchmark, 12-Month Average, Actual Hardware Meter Reading, and **Official SP / City Energy Piped Gas Tariff** (`SGD/kWh` via `spgroup.com.sg` & SingStat `M891051`).
  - **Tengah Centralized Cooling (`kWh` & Per-Coil `°C`):** Cumulative, Today, and Month-to-Date cooling energy consumption (`/frosty/consumption`), plus automatic discovery of paired per-room **Fan Coil Units (FCUs)** with live room temperature (`°C`), setpoint, on/off state, online status, and operation mode.
- **Billing, Payments, EV Charging & Account Ecosystem:**
  - **Billing & Payments:** Outstanding Bill Balance (`SGD`), Latest Bill Amount (UTC instant-sorted, with PDF URL & billing period attributes), Latest Bill Due Date (`date` device class), Latest Payment Amount (method, channel, recurring flag, transaction ID), Prepaid (PPMS) Credit Balance, Next Scheduled Meter Reading Date, and Electricity Retailer / Premise Info.
  - **SP EV Charging & Loyalty (Auto-Discovered When Enrolled):** EV Wallet Points & Dollar Balance (`Tyche`), Latest EV Charging Session, EV Last Charge (`kWh` & receipt cost/address), EV Unpaid Orders (`Eva`), Green Goals Electricity & Water Used + % vs. Last Year, SP GreenUP Leaves & Tier, and Unread In-App Notifications count.
  - **Binary Sensors:** GIRO Enabled, Recurring Card Payment, Paperless e-Bill, Smart Meter (AMI) Installed, Self Meter Reading Window Open, and **Smart Meter Usage Anomaly** (`problem` device class triggered when SP's AMI metadata flags water leaks or abnormal spikes).
- **Hybrid 13-Month Backfill + Live 48-Hour Recorder Rewrite (Spike-Free Energy Dashboard):**
  - **One-Time 13-Month History on First Install:** Merges 13 months of daily AMI history with ~100 days (maximum SP retention) of half-hourly AMI slots — plus monthly non-AMI billed usage and `last_bill` utility bill statistics — into Home Assistant Recorder external statistics once on initial setup.
  - **Live 48-Hour Sliding-Window Backfill:** Every poll re-anchors strictly prior to `yesterday 00:00` and updates the live 48-hour window (`yesterday` + `today`), placing SP's ~5-hour-delayed 4-hour smart meter batches into their exact historical hours without spikes or restarts.
- **Security, Hardening & Diagnostics:**
  - **Zero Plaintext Password Storage:** Your password is exchanged once for OAuth tokens during setup/reauth/reconfigure and never persisted to `.storage` (legacy stored passwords are automatically scrubbed on load).
  - **Complete Auth0 MFA & Anti-Lockout Protection:** Supports Authenticator App (`6-digit TOTP`), **SMS OTP**, and **Email OTP**, 60-second failed-login cooldowns, graceful handling of Auth0 `requires_verification` challenges without destructive reauth loops, PII-redacted URL logging, 8 MB bounded `utf-8-sig` responses, `Decimal` half-up cent rounding, UI `reconfigure` flow, and one-click PII-redacted **Home Assistant Diagnostics**.

## Installation

### Option 1: HACS (Recommended)
1. Open **HACS -> Integrations -> Custom repositories** (top-right menu).
2. Add this repository URL with category **Integration**, then install **Singapore Power (SP Group)**.
3. Restart Home Assistant.

### Option 2: Manual Installation
1. Copy `custom_components/singapore_power` into `config/custom_components/singapore_power/` on your Home Assistant instance.
2. Restart Home Assistant.

### Setup
1. Go to **Settings -> Devices & Services -> Add Integration** and search for **Singapore Power**.
2. Sign in with your SP App email and password, and complete TOTP, SMS OTP, or Email OTP verification if prompted.

## Energy Dashboard Setup

In **Settings -> Dashboards -> Energy**, configure Electricity, Gas, and Water using either:
- **Time-Aligned External Statistics (Recommended):**
  - `singapore_power:<account>_electricity_consumption` (paired with `singapore_power:<account>_electricity_cost`)
  - `singapore_power:<account>_water_consumption` (paired with `singapore_power:<account>_water_cost`)
  - `singapore_power:<account>_gas_consumption` (paired with `singapore_power:<account>_gas_cost`)
  - Monthly bill history is also available in Statistics Charts under `singapore_power:<account>_last_bill`.
- **Live Cumulative Sensor Entities:** `sensor.sp_<account>_electricity`, `sensor.sp_<account>_water`, `sensor.sp_<account>_gas`.
