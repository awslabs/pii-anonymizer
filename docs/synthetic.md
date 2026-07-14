# Synthetic PII Generation (Step 2)

The synthetic step replaces all detected PII with realistic fake data. It runs as a single Lambda invocation per job: all files' detections are aggregated, deduplicated, and processed together so the same PII value always maps to the same synthetic replacement across every file in the job.

## Pipeline Flow

```
Detection JSONs (all files)
  → Read from S3
  → Deduplicate by content
  → Categorize (value patterns → LLM type hint fallback)
  → Cluster within category (names, phones, addresses grouped by identity)
  → Merge super-groups (person = name + email, address = street + city + state + zip)
  → Dynamic batching (split if > max_synthetic_batch_tokens)
  → LLM generation (concurrent batches via ThreadPoolExecutor)
  → 3-phase repair (missed → inconsistent → collisions)
  → Post-process consistency (name variants, phone formats, SSN partials)
  → Validate (every unique PII has a mapping, remove hallucinated keys)
  → Store synthetic_mapping.json to S3
```

DynamoDB status progression: `DETECT_COMPLETE` → `GENERATING_SYNTHETIC` → `SYNTHETIC_COMPLETE`

## Blackout Mode Shortcut

When `redaction.mode: "blackout"` in config, the entire LLM pipeline is skipped:

```python
# synthetic_pii_generator.py → batch_generate_synthetic_pii()
if redaction_mode == "blackout":
    return {d.get("content", ""): "[REDACTED]" for d in pii_detections if d.get("content")}
```

No LLM calls, no clustering, no repair. Every PII value maps to `[REDACTED]`.

---

## Category Inference

`value_categorizer.py` assigns each PII value a category using a two-tier approach. The category determines which clustering algorithm groups related values together.

### Tier 1: Structural Pattern Matching (checked first)

High-precision regex/structural checks on the value itself, no LLM type hint needed:

| Category      | Detection Logic                                                             | Examples                            |
| ------------- | --------------------------------------------------------------------------- | ----------------------------------- |
| `email`       | `user@domain.tld` regex                                                     | `john.smith@hospital.com`           |
| `ssn`         | `XXX-XX-XXXX`, masked `***-**-XXXX`, `ending in XXXX`                       | `123-45-6789`, `***-**-1234`        |
| `financial`   | Currency symbol + digits, or `digits.XX`                                    | `$45,230.00`, `38715`               |
| `date`        | 6 date patterns (MM/DD/YYYY, YYYY-MM-DD, Month Day Year, DD-Mon-YYYY, etc.) | `01/15/2024`, `January 15, 2024`    |
| `phone`       | 10-15 digits with phone punctuation `+()-./`                                | `(555) 123-4567`, `+1-555-123-4567` |
| `address`     | Street number + street suffix (St, Ave, Blvd, etc.) or P.O. Box             | `123 Main St, City, ST 12345`       |
| `org_name`    | Contains org keywords (Hospital, LLC, Bank, University, etc.)               | `Memorial Hospital`, `Acme Corp`    |
| `person_name` | 2-4 capitalized words, no org/address keywords                              | `John Smith`, `Dr. Sarah Chen`      |

Checks run in this exact order. First match wins: a value like `Dr. Smith Hospital` hits `org_name` (contains "Hospital") before `person_name`.

### Tier 2: LLM Type Hint Mapping (fallback)

If no structural pattern matches, the LLM's type label from Step 1 detection is mapped via `config.yaml`:

```yaml
# config.yaml → clustering.type_hint_map
clustering:
  type_hint_map:
    address: [address, street, city, state, zip, location]
    person_name: [name, first_name, last_name, patient_name, borrower, attorney]
    phone: [phone, fax, telephone]
    ssn: [ssn, social]
    date: [dob, birth, expiration_date]
    org_name: [institution, hospital, clinic, company, bank, employer, court]
    id_generic:
      [
        id,
        record,
        account,
        policy,
        reference,
        case,
        npi,
        license,
        passport,
        vin,
        credit_card,
        ein,
        tax_id,
        docket,
        credit_score,
      ]
```

Matching is bidirectional substring: if the LLM says `patient_name`, it matches `name` in the `person_name` list. Add keywords here to handle new PII types without code changes.

---

## Clustering

After categorization, values within each category are clustered so related values get consistent synthetic replacements. Each category has its own clustering strategy:

| Category      | Strategy                | Grouping Logic                                                                                                                                      |
| ------------- | ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `person_name` | Token overlap           | 2+ shared tokens for multi-word names; substring match for single-word. Titles (Mr, Dr, MD) stripped before comparison.                             |
| `address`     | Street components       | Street number must match exactly. Street name token similarity ≥50%. Fragments (city-only, state-only) matched by substring against full addresses. |
| `org_name`    | Token set similarity    | ≥90% token overlap (after stripping LLC/Inc/Corp). Substring match for short names (≤2 tokens).                                                     |
| `phone`       | Normalized digits       | Stripped to last 10 digits. `(555) 123-4567` and `+1-555-123-4567` → same cluster.                                                                  |
| `ssn`         | Normalized 9 digits     | Masked forms (`***-**-XXXX`) excluded from normalization but linked later in post-processing.                                                       |
| `date`        | Normalized ISO          | All formats parsed to `YYYY-MM-DD`. `01/15/2024` and `January 15, 2024` → same cluster.                                                             |
| `email`       | Lowercased exact        | `John@Example.COM` and `john@example.com` → same cluster.                                                                                           |
| `financial`   | Digits + decimal        | `$45,230.00` and `$45,230` → same cluster (both normalize to `45230`).                                                                              |
| `id_generic`  | Alphanumeric lowercased | `PT-12345` and `pt12345` → same cluster.                                                                                                            |

Max items per cluster: `clustering.max_cluster_size` (default 10). Prevents snowball merging where unrelated values chain-link into one giant cluster.

### Super-Groups

Before batching, related categories are merged into super-groups so the LLM generates cross-type consistent values in a single call:

```python
SUPER_GROUPS = {
    "person": ["name", "first_name", "last_name", "middle_name", "email"],
    "address": ["address", "city", "state", "zip"],
}
```

This ensures:

- A person's name and email are generated together → `sarah.johnson@company.com` derives from `Sarah Johnson`
- Address components (street, city, state, zip) are generated together → consistent synthetic address

Items keep their original type labels: super-groups only control batching, not category inference.

---

## LLM Generation

### Prompt Structure

Two prompts from `prompts.py`:

**`BATCH_SYNTHETIC_SYSTEM_PROMPT`**: rules for the LLM:

- Preserve format exactly (word count, punctuation, case)
- Preserve gender (female stays female)
- Match character length ±20%
- Never add extensions/prefixes not in original
- Preserve alphabetic prefixes in IDs (`NTN-65647` → `NTN-83491`, not `KPR-83491`)
- Entity groups: one synthetic identity per group, derive all variants from it
- Uniqueness: different entities must get different synthetics

**`BATCH_SYNTHETIC_TASK_PROMPT_TEMPLATE`**: the actual data, built by `_build_batch_prompt()`:

- **Values section**: each entity group with category label, items numbered, cluster members marked "(SAME entity)", per-category instructions from `CATEGORY_INSTRUCTIONS`
- **Replacements section**: same structure as template for LLM to fill

The LLM returns XML:

```xml
<synthetic_data>
  <names>
    <item>
      <original>John Smith</original>
      <synthetic>Robert Chen</synthetic>
    </item>
  </names>
</synthetic_data>
```

### XML Parsing

`parse_synthetic_data()` handles:

- Markdown-wrapped XML (` ```xml ... ``` `)
- Escaped ampersands (`&` → `&amp;`)
- Truncated responses (missing `</synthetic_data>` → raises error, triggers repair)
- Uses `defusedxml.ElementTree` for safe parsing

### Dynamic Batching

`_split_into_batches()` checks if all PII fits in one LLM call:

```python
# Measures actual prompt size (not just raw values)
# 3x multiplier: prompt has values twice + LLM output repeats in XML
prompt_tokens = estimate_tokens(full_prompt, chars_per_token)
if prompt_tokens * 3 <= max_batch_tokens:
    return [pii_by_type]  # single batch, one LLM call
```

If it exceeds `max_synthetic_batch_tokens` (default 10000), PII is split into multiple batches preserving type group boundaries: all items of a given type stay together for context.

Batches run concurrently via `ThreadPoolExecutor` using `concurrency.max_workers` (default 6).

### Inference Parameters

Synthetic generation uses higher temperature than detection for diverse output:

```yaml
# config.yaml
synthetic:
  temperature: 0.8 # Creative, diverse synthetic data (detection uses 0)
  top_k: 200 # Wide token choices (detection uses 5)
  max_tokens: 64000 # Clamped to the model's output limit (e.g. Nova → 10000)
  reasoning_effort: "none" # OpenAI GPT-5.x only; "none" recommended (creative step)
  enable_thinking: false # Claude only; ignored by Nova/OpenAI
```

> Synthetic is a creative step, so reasoning/thinking add little value. `reasoning_effort: none` is the recommended default for GPT, and `enable_thinking` (Claude only) is off by default. Both are read per-model: OpenAI uses `reasoning_effort`, Claude uses `enable_thinking`, Nova uses neither.

---

## 3-Phase Repair

After the main LLM generation, `_repair_with_llm()` fixes three types of issues:

### Phase 1: Missed Items

PII values the LLM didn't return a mapping for. Common when XML response gets truncated on large batches.

### Phase 2: Inconsistent Values

Synthetic value equals the original: the LLM returned the input back unchanged. Detected by comparing `pii_mapping[orig] == orig`.

### Phase 3: Collisions

Multiple different original values mapped to the same synthetic value. First mapping is kept; the rest are sent to repair.

### Repair Process

1. Items grouped by super-group (person, address, or individual type)
2. Per group: existing mappings provided as context + items to fix
3. LLM generates replacements using `BATCH_REPAIR_SYSTEM_PROMPT` / `BATCH_REPAIR_PROMPT_TEMPLATE`
4. Validation per item:
   - Must not be empty
   - Must not equal the original
   - Must not reuse any existing synthetic value
5. Failed items fall back to Faker (up to 10 attempts to avoid collisions with existing synthetics)

---

## Faker Fallback

`generate_synthetic_pii_fallback()` generates synthetic data without LLM when:

- LLM call fails entirely
- Repair LLM doesn't return a value
- Need to avoid collision with existing synthetics

Format-preserving generation per PII type:

| Type              | Faker Logic                                                                                                    |
| ----------------- | -------------------------------------------------------------------------------------------------------------- |
| **Names**         | Preserves word count. Gender detection via common female names list + titles (Ms., Mrs.).                      |
| **SSN**           | Preserves format: dashed (`XXX-XX-XXXX`), undashed, masked (`***-**-XXXX`), "ending in XXXX".                  |
| **Phone**         | Preserves exact format: `+1`, parentheses, dots, dashes, extensions (`ext.`), pager notation.                  |
| **Date**          | Preserves format: `MM/DD/YYYY`, `DD-Mon-YYYY`, `Month Day, Year`.                                              |
| **Address**       | Preserves structure: street-only vs full (city/state/zip). Detects comma presence.                             |
| **Age patterns**  | Detects `35 yo M`, `42 y/o F` formats. Generates new age within ±15 range, preserves format.                   |
| **Account/ID**    | Preserves digit/letter/separator pattern exactly. Alphabetic chars → random uppercase, digits → random digits. |
| **Credit scores** | Generates in same range bracket (300-579, 580-669, 670-739, 740-799, 800-850).                                 |
| **Institution**   | Detects subtype (hospital, medical center, clinic). Generates matching institution name.                       |
| **Generic**       | Replaces digit sequences with same-length random digits. Preserves all non-digit characters.                   |

---

## Post-Processing Consistency

`_post_process_consistency()` runs after all mappings are generated to ensure related synthetic values are consistent across entity variants.

### Name Variant Linking

Groups name variants by shared tokens using iterative merging:

```
"Sarah Elizabeth Johnson", "Sarah Johnson", "Johnson", "Ms. Johnson", "S.E.J."
  → all linked to one group
```

- Anchor = longest variant (most words). All others derive from anchor's synthetic.
- Word-by-word mapping: `Sarah` → `Laura`, `Johnson` → `Bennett`
- Initials: `S.E.J.` → `L.M.B.` (derived from first letters of synthetic name)
- Prefix matching for fuzzy cases (e.g., `Sara` matches `Sarah`)

### Phone Format Consistency

Groups by normalized 10 digits. All format variants derive from one synthetic number:

```
(555) 123-4567  → (734) 229-4081
555-123-4567    → 734-229-4081
+1-555-123-4567 → +1-734-229-4081
5128675310      → 7342294081
```

Preserves: `+1` prefix, parentheses, dots, dashes, extensions, pager notation.

### SSN Partial Consistency

Full SSN and masked forms share the same last 4 digits:

```
123-45-6789     → 987-65-4321
***-**-6789     → ***-**-4321
ending in 6789  → ending in 4321
```

### Punctuation Normalization

Values differing only by punctuation/whitespace get the same synthetic:

```
"4521 Oak Street, Austin, TX"  and  "4521 Oak Street Austin TX"
  → same synthetic value
```

---

## Institution Name Handling

Institution names get special treatment because they often appear in multiple forms within a document:

1. `identify_related_entities()` extracts base entity by stripping department info (after comma) and location (after "of")
2. Groups: `"Memorial Hospital"`, `"Memorial Hospital, Dept of Surgery"`, `"Memorial Hospital of Washington"` → base = `"Memorial Hospital"`
3. One synthetic generated for the base entity
4. Variants constructed by replacing the base part while preserving structure:
   - `"Memorial Hospital, Dept of Surgery"` → `"Evergreen Medical Center, Dept of Surgery"`
   - `"Memorial Hospital of Washington"` → `"Evergreen Medical Center of Washington"`

---

## Output Format

Stored at `{output_prefix}/intermediate/synthetic/synthetic_mapping.json`:

```json
{
  "job_id": "batch-test",
  "pii_mapping": {
    "John Smith": "Robert Chen",
    "123-45-6789": "987-65-4321",
    "***-**-6789": "***-**-4321",
    "(555) 123-4567": "(734) 229-4081"
  },
  "stats": {
    "total_detections": 150,
    "unique_values": 45,
    "duplicates_removed": 105,
    "mappings_generated": 43,
    "unmapped": 2,
    "elapsed_seconds": 12.3
  },
  "token_usage": {
    "detection": {
      "input_tokens": 50000,
      "output_tokens": 20000,
      "requests": 15
    },
    "synthetic": { "input_tokens": 8000, "output_tokens": 3000, "requests": 2 },
    "total": { "input_tokens": 58000, "output_tokens": 23000, "requests": 17 }
  }
}
```

Token usage includes both detection (aggregated from Step 1 JSONs) and synthetic generation totals. Cost estimation available via `TokenTracker.estimate_cost()` using `pricing.yaml`.

---

## Validation Guards

| Guard                         | Where             | What It Does                                                                                |
| ----------------------------- | ----------------- | ------------------------------------------------------------------------------------------- |
| `validate_synthetic_input()`  | Before generation | Filters detections with empty `content`. Assigns `"other"` type if `type` missing.          |
| `validate_synthetic_output()` | After generation  | Logs any unique PII values that have no mapping.                                            |
| Hallucination removal         | After generation  | Removes keys from `pii_mapping` that don't exist in the detection list (LLM invented them). |
| Final count check             | After generation  | Logs error if `len(pii_mapping) != len(unique_values)`.                                     |

---

## Error Handling

- If the entire synthetic Lambda fails, DynamoDB status set to `FAILED` with error details and `failed_step: "synthetic"`
- Step Functions retries on throttling/timeout: 5 attempts, 10s interval, 2.5x exponential backoff
- Individual batch failures within `ThreadPoolExecutor` are caught: Faker fills missing mappings
- `check_and_raise_throttling()` detects Bedrock throttling errors and re-raises for SF retry
- Boto3 client configured with adaptive retry mode, 5 max attempts, 300s read timeout

---

## S3 Access

| Operation   | Bucket | Purpose                          |
| ----------- | ------ | -------------------------------- |
| `GetObject` | Output | Read detection JSONs from Step 1 |
| `PutObject` | Output | Write `synthetic_mapping.json`   |

---

## Config Reference

```yaml
# Step 2 inference parameters
synthetic:
  temperature: 0.8 # Higher = more diverse synthetic data
  top_k: 200 # Wider token choices
  max_tokens: 64000 # Max output tokens per LLM call
  enable_thinking: false # Extended thinking (increases latency/cost)
  thinking_budget_tokens: 4000

# Batching and concurrency
concurrency:
  max_workers: 6 # Threads for concurrent batch processing
  max_synthetic_batch_tokens: 10000 # Token limit before splitting into batches
  chars_per_token: 4 # Token estimation ratio

# Clustering
clustering:
  max_cluster_size: 10 # Max items per cluster
  type_hint_map: # LLM type label → category mapping
    person_name: [name, first_name, last_name, ...]
    address: [address, street, city, state, zip, ...]
    # ... (see config.yaml for full list)

# Redaction mode (affects synthetic step)
redaction:
  mode: "synthetic" # "synthetic" = LLM generation, "blackout" = skip LLM
```

---

## Key Files

| File                              | Purpose                                                                                                                                  |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `handlers/synthetic_handler.py`   | Lambda entry point. Reads detection JSONs, deduplicates, orchestrates pipeline, stores mapping.                                          |
| `core/synthetic_pii_generator.py` | `batch_generate_synthetic_pii()`: main entry. Batching, LLM calls, repair, Faker fallback, post-processing.                             |
| `core/value_categorizer.py`       | `infer_category()`: two-tier category inference. `cluster_items_by_category()`: category-specific clustering. Normalization functions. |
| `core/prompts.py`                 | `BATCH_SYNTHETIC_*` prompts, `BATCH_REPAIR_*` prompts, `CATEGORY_INSTRUCTIONS` dict.                                                     |
| `helpers/model_config_helper.py`  | `get_creative_config_from_yaml()`: synthetic inference params. `get_concurrency_config()`: batching config.                            |
| `helpers/token_tracker.py`        | Thread-safe token accumulator. Cost estimation via `pricing.yaml`.                                                                       |
| `helpers/text_chunker.py`         | `estimate_tokens()`: token count estimation for batch splitting.                                                                        |
| `validation/model_schemas.py`     | `validate_synthetic_input()`, `validate_synthetic_output()`: input/output guards.                                                       |
