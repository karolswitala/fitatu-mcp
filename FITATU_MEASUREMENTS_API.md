# Fitatu Body Measurements API

Standalone reference for the Fitatu body-measurements REST API (weight, body sizes,
body fat) plus the derived BMI calculation. Everything here was observed from live
traffic of the official Fitatu app (v4.16.0) and web client against the production
backend. It is sufficient to implement a read-only client without any other document.

> **Scope:** read (GET) operations only. Write operations exist in the app but are out of
> scope here. Opening a day's detail view in the app also fires a `PUT /measurements/{date}`
> that re-saves identical values — a client reading data should **not** replicate it.

---

## 1. Overview

| | |
|---|---|
| **Base URL** | `https://pl-pl.fitatu.com` |
| **API root** | `https://pl-pl.fitatu.com/api` |
| **Protocol** | HTTPS, HTTP/2 (HTTP/1.1 also works) |
| **Content type** | `application/json` (UTF-8) |
| **Per-user scope** | every measurement path is nested under `/api/users/{user_id}` |
| **Host region** | `pl-pl` = Poland locale cluster; the host encodes the region |

`{user_id}` is the numeric Fitatu account id (e.g. `12345678`). It is returned by the
login response and is also embedded in the auth JWT (`user_id`/`uid`/`id`/`sub` claim).

---

## 2. Authentication

All requests require a bearer token **plus** a set of app-identifying headers. Requests
missing the `api-key`/`api-secret` pair are rejected.

### 2.1 Headers

| Header | Value | Notes |
|---|---|---|
| `Authorization` | `Bearer <access_token>` | Per-user session JWT from login. **Secret.** |
| `api-key` | `FITATU-MOBILE-APP` | Static app constant (same for every install). |
| `api-secret` | `<FITATU_API_SECRET>` | Static app constant. Required; 401/403 without it. Supply via the `FITATU_API_SECRET` env var — never commit the value. |
| `API-Cluster` | `pl-pl{user_id}` | Routing hint. |
| `app-os` | `FITATU-WEB` | Client platform. |
| `app-version` | e.g. `4.5.4` | Client version. |
| `app-uuid` | `64c2d1b0-c8ad-11e8-8956-0242ac120008` | Device/install UUID. |
| `accept` | `application/json; version=v3` | API version negotiation. |
| `content-type` | `application/json` | — |

> The `api-key`/`api-secret` values are app-level constants baked into the client (not
> user credentials). The only per-user secret is the `Authorization` bearer token.

### 2.2 Obtaining / refreshing the token

| Action | Endpoint |
|---|---|
| Login | `POST /api/login` — body `{"_username": "...", "_password": "..."}` → `{token, refresh_token, …}` |
| Refresh | `POST /api/token/refresh` — body `{"refresh_token": "..."}` → `{token, …}` |

On `401 Unauthorized`, refresh the token (or re-login) and retry once.

### 2.3 Status codes

| Code | Meaning |
|---|---|
| `200` | Success. |
| `401` | Missing/expired bearer token → refresh & retry. |
| `403` | Missing/invalid `api-key`/`api-secret`, or accessing another user's data. |
| `404` | No data for the given date/part. |
| `5xx` | Backend error — retry with backoff. |

---

## 3. Conventions

- **Dates:** `YYYY-MM-DD` (e.g. `2026-03-01`), interpreted in the user's timezone.
- **Ordering:** list responses are **newest-first** (descending date).
- **Pagination:** `limit` (max items) and `page` (1-based). `limit=500` effectively returns
  all history for typical accounts.
- **Units:** weight `KG`, body sizes `CM`, body fat `%` (percent). The user's unit system is
  in `settings-new` (`weightUnit`, `heightUnit`, `sizeUnit`). All examples here are metric.
- **Numbers:** JSON numbers; integers where the value is whole (e.g. `37`), decimals otherwise
  (`37.5`). Treat all as floats.

### 3.1 Metrics / body parts

| Canonical | API key | App (UI) label | Unit | Has size-series endpoint |
|---|---|---|---|---|
| weight | *(weight endpoints)* | Body weight | KG | via weight endpoints |
| neck | `neck` | Neck | CM | yes |
| chest | `chest` | Chest | CM | yes |
| waist | `waist` | Waist | CM | yes |
| abdomen | `stomach` | Abdomen | CM | yes |
| hips | `hips` | Hips | CM | yes |
| thigh | `thigh` | Thigh | CM | yes |
| calf | `calf` | Calf | CM | yes |
| biceps | `biceps` | Arm/bicep | CM | yes |
| body_fat | `fatPercentage` | The body fat | % | yes |
| BMI | *(none — computed)* | BMI | kg/m² | derived from weight + height |

> Note the two name mismatches: **Abdomen → `stomach`**, **Arm/bicep → `biceps`**,
> **The body fat → `fatPercentage`**.

---

## 4. Endpoints

### 4.1 Weight — summary / series

```
GET /api/users/{user_id}/measurements/summary/weight
```

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | — | Max entries. Use `1` for latest only, `500` for full history. |
| `page` | int | `1` | Page number (1-based). |
| `fromDate` | `YYYY-MM-DD` | — | Inclusive lower date bound; returns entries on/after this date (newest-first), capped by `limit`. Used by the date-range chips. |

**Response** — array, newest-first:

```json
[
 { "date": "2026-03-15", "value": 70.0, "difference": 0,    "direction": 0 },
 { "date": "2026-03-14", "value": 70.0, "difference": -0.2, "direction": -1 },
 { "date": "2026-03-13", "value": 70.2, "difference": 0,    "direction": 0 },
 { "date": "2026-03-12", "value": 70.2, "difference": -0.3, "direction": -1 }
]
```

**Fields**

| Field | Type | Description |
|---|---|---|
| `date` | string | Measurement date. |
| `value` | float | Weight in KG. |
| `difference` | float | Change vs. the previous (older) recorded entry. |
| `direction` | int | Sign of change: `-1` down, `0` unchanged, `1` up. |

**Examples**

```bash
# Latest weight only
GET /api/users/12345678/measurements/summary/weight?limit=1&page=1
# → [ { "date": "2026-03-15", "value": 70.0, "difference": 0, "direction": 0 } ]

# Full history
GET /api/users/12345678/measurements/summary/weight?limit=500&page=1

# Bounded range (e.g. a date-range chip)
GET /api/users/12345678/measurements/summary/weight?fromDate=2026-01-01&limit=41&page=1
```

---

### 4.2 Weight — dense daily chart

```
GET /api/users/{user_id}/measurements/chart/weight
```

No parameters. Returns a dense date→weight map for charting (one entry per day that has a
value). Powers the weight graph.

**Response**

```json
{
 "weights": {
  "2026-03-15": 70.0,
  "2026-03-14": 70.0,
  "2026-03-13": 70.2,
  "2026-03-12": 70.2,
  "2026-03-10": 70.5
 }
}
```

| Field | Type | Description |
|---|---|---|
| `weights` | object | Map of `"YYYY-MM-DD"` → weight (KG). Not ordered; sort keys client-side. |

> This is the cheapest way to fetch the full weight series (one request). Unlike
> `summary/weight`, it carries no `difference`/`direction` (recompute from neighbours).

---

### 4.3 Body sizes — summary (all parts)

```
GET /api/users/{user_id}/measurements/summary/size
```

No parameters. Returns first vs. latest value and total change for every body-size metric.

**Response**

```json
{
 "neck":          { "startValue": 36.5, "endValue": 37,   "difference": 0.5 },
 "chest":         { "startValue": 100,  "endValue": 96,   "difference": -4 },
 "waist":         { "startValue": 90,   "endValue": 82,   "difference": -8 },
 "stomach":       { "startValue": 97,   "endValue": 88,   "difference": -9 },
 "hips":          { "startValue": 101,  "endValue": 95,   "difference": -6 },
 "thigh":         { "startValue": 58,   "endValue": 55,   "difference": -3 },
 "calf":          { "startValue": 39,   "endValue": 37.5, "difference": -1.5 },
 "biceps":        { "startValue": 33,   "endValue": 32,   "difference": -1 },
 "fatPercentage": { "startValue": 25.0, "endValue": 20.4, "difference": -4.6 }
}
```

| Field | Type | Description |
|---|---|---|
| `<part>` | object | Keyed by API part name (see §3.1). |
| `startValue` | float | Earliest recorded value. |
| `endValue` | float | Latest recorded value. |
| `difference` | float | `endValue − startValue`. |

> Weight is **not** in this response — use the weight endpoints (§4.1/§4.2).

---

### 4.4 Body sizes — series for one part

```
GET /api/users/{user_id}/measurements/size/{part}
```

**Path parameters**

| Param | Description |
|---|---|
| `part` | One of the API keys: `neck, chest, waist, stomach, hips, thigh, calf, biceps, fatPercentage`. |

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | — | Max entries (`500` = all history). |
| `page` | int | `1` | Page number. |

**Response** — array, newest-first:

```json
[
 { "date": "2026-03-01", "value": 96 },
 { "date": "2026-02-15", "value": 97 },
 { "date": "2026-02-01", "value": 97 },
 { "date": "2026-01-15", "value": 98.5 }
]
```

| Field | Type | Description |
|---|---|---|
| `date` | string | Measurement date. |
| `value` | float | Value in the part's unit (CM, or % for `fatPercentage`). |

**Example**

```bash
GET /api/users/12345678/measurements/size/chest?limit=500&page=1
```

---

### 4.5 Day detail — all metrics for one date

```
GET /api/users/{user_id}/measurements/{date}
```

**Path parameters**

| Param | Description |
|---|---|
| `date` | `YYYY-MM-DD`. The day to read. |

**Response**

```json
{
 "weight": 71.2,
 "neck": 37,
 "chest": 96,
 "waist": 82,
 "stomach": 88,
 "hips": 95,
 "thigh": 55,
 "calf": 37.5,
 "biceps": 32,
 "fatPercentage": 20.4,
 "weightUnit": "KG",
 "sizeUnit": "CM"
}
```

| Field | Type | Description |
|---|---|---|
| `weight` | float | KG. |
| `neck … biceps` | float | Body sizes in CM. |
| `fatPercentage` | float | Body fat %. |
| `weightUnit` | string | `KG` (or user's unit). |
| `sizeUnit` | string | `CM` (or user's unit). |

> Metrics with no entry for that date may be absent/null. Opening this resource in the app
> triggers a follow-up `PUT` with the same values — not required for reading.

---

### 4.6 User settings — height (for BMI)

```
GET /api/users/{user_id}/settings-new/{date}
```

Returns a large settings object; the fields relevant to measurements live under
`userSettings`:

```json
{
 "userSettings": {
  "height": 175.0,
  "heightCm": 175,
  "heightUnit": 1
 }
}
```

| Field | Type | Description |
|---|---|---|
| `userSettings.heightCm` | int | Height in centimetres. |
| `userSettings.height` | float | Height in the user's unit. |
| `userSettings.heightUnit` | int | `1` = metric (cm). |

Height changes rarely; fetch once (e.g. for today) and cache.

---

## 5. Derived metric — BMI

BMI has **no endpoint**; the app computes it client-side:

```
BMI = weight_kg / (height_cm / 100)²
```

Example: `weight = 70.0 kg`, `height = 175 cm`
→ `70.0 / (1.75)² = 70.0 / 3.0625 = 22.9`.

For a BMI series, pair each weight point (§4.1/§4.2) with the (near-constant) height.

---

## 6. Quick recipes

| Goal | Calls |
|---|---|
| Latest weight | `summary/weight?limit=1` |
| Full weight history (dense) | `chart/weight` |
| Weight history with deltas | `summary/weight?limit=500` |
| All body-size current values + totals | `summary/size` |
| One metric over time | `size/{part}?limit=500` (or `chart/weight` for weight) |
| Everything for a specific day | `measurements/{date}` |
| BMI for a day | `measurements/{date}` (weight) + `settings-new/{date}` (height), then §5 |
| Full history snapshot (all metrics) | `chart/weight` + `size/{part}`×9 + `settings-new/{today}` ≈ 11 requests |

---

## 7. Full worked example

```bash
BASE=https://pl-pl.fitatu.com
UID=12345678
TOKEN="<bearer access token>"

curl -s "$BASE/api/users/$UID/measurements/summary/weight?limit=1&page=1" \
  -H "Authorization: Bearer $TOKEN" \
  -H "api-key: FITATU-MOBILE-APP" \
  -H "api-secret: <FITATU_API_SECRET>" \
  -H "API-Cluster: pl-pl$UID" \
  -H "app-os: FITATU-WEB" \
  -H "app-version: 4.5.4" \
  -H "app-uuid: 64c2d1b0-c8ad-11e8-8956-0242ac120008" \
  -H "accept: application/json; version=v3"
# → [ { "date": "2026-03-15", "value": 70.0, "difference": 0, "direction": 0 } ]
```

---

## 8. Caveats

- **`fromDate` semantics** are inferred (inclusive lower bound, newest-first, capped by
  `limit`); verify against live data if exact windowing matters.
- **`difference`/`direction`** on weight are relative to the previous recorded entry, not a
  fixed baseline.
- All example values are synthetic and illustrative (not real user data); substitute your
  own `{user_id}` and token.
```
