# Rental Deposit Court — GenLayer Intelligent Contract

A rental-deposit contract settled by **GenLayer validator consensus**
when landlord and tenant disagree at checkout. A tenant locks GEN as a
deposit; at checkout the landlord claims deductions (damage, cleaning,
unpaid rent) with evidence; the tenant confirms the claim (the landlord
keeps the claimed amount, the rest returns to the tenant) or disputes
it. A disputed claim is adjudicated by an LLM adjudicator running
under GenLayer's equivalence principle: the leader judges the evidence,
every other validator independently re-runs the same adjudication and
compares the decision fields, and only an agreed outcome moves funds —
full claim kept, full refund, or a split.

Built for submission as an **Intelligent Contracts** contribution on the
GenLayer Portal (account `turoh`) — the second weekly slot, after
Adjudicated Escrow (submitted 2026-11-26, pending review). No
submission has been done from this repo — local work + a StudioNet
deploy only.

## Why this qualifies (and is not a thin wrapper)

- The consensus decision is the core of the contract: a fixed rubric
  over the agreed lease, the landlord's checkout evidence (including a
  fetched evidence page), and the tenant's dispute claim — with a real
  validator that re-runs the whole adjudication and compares verdict,
  landlord share, and confidence.
- A full state machine with per-stage deadlines and terminal states.
- Edge cases are handled in-contract: empty evidence, claims larger
  than the deposit, double claims, double disputes, double settlement,
  timeouts on both sides, dead evidence links, and unparsable LLM
  output (mapped to a canonical `[LLM_ERROR]`).
- Rejected patterns (per the contribution guidelines): no hello-world,
  no simple storage, no format-only validator, no boilerplate fork. The
  LLM is not asked to "decide X" in the abstract — it applies a written
  rubric to concrete evidence, and its answer is checked by independent
  re-execution.

## State machine

Per lease (stored as a JSON record in `TreeMap[u256, str]`):

```
FUNDED ──► CHECKOUT_CLAIMED ──► CONFIRMED   (tenant confirms: landlord
   │              │                          keeps the claimed amount,
   │              │                          remainder returns to tenant)
   │              ├──► DISPUTED ──► ADJUDICATED  (release / refund / split)
   │              │
   │              └──► CONFIRMED   (tenant silent past the dispute
   │                                 window: landlord collects the claim)
   └──► REFUNDED   (landlord never claimed by the checkout deadline:
                     tenant refunded in full)
```

- **Deadlines**: checkout window 1–180 days, dispute window 1–30 days
  (both set at creation). The clock is the deterministic transaction
  time (`datetime.now(timezone.utc)` — GenVM pins it to the transaction
  datetime, so all validators agree).
- **Settlement is written before any transfer**: the record moves to its
  terminal state (`CONFIRMED` / `ADJUDICATED` / `REFUNDED`) and the
  `landlord_paid` / `tenant_paid` amounts are stored *before*
  `emit_transfer` is called, so a settled lease can never pay out twice.
- **Money is atto-scale `u256`** (GEN × 10¹⁸). Confirmed claims pay the
  exact claimed amount (`landlord_paid = claim_amount`,
  `tenant_paid = amount - claim_amount`); adjudicated splits use
  `landlord_amount = amount * landlord_bps // 10000`,
  `tenant_amount = amount - landlord_amount` (no dust lost).

## Consensus design (`adjudicate`)

1. **Anyone** may trigger adjudication on a `DISPUTED` lease.
2. **Leader** (`evaluate`): fetches the landlord's evidence URL with
   `gl.nondet.web.render(mode="text")`, truncated to 8,000 chars. A dead
   link weakens the landlord's case but does not block adjudication —
   the text evidence is still judged. Then it calls
   `gl.nondet.exec_prompt` with `response_format="json"` and a fixed
   rubric:
   - `release` — checkout evidence credibly shows the claimed
     deductions as described in the lease terms; the dispute names no
     concrete failure. Landlord keeps the full claimed amount
     (`landlord_bps` = the claim's share of the deposit).
   - `refund` — evidence missing, vague, or mismatched, or the dispute
     names a concrete failure (no damage, pre-existing condition,
     normal wear and tear). Tenant refunded the full deposit
     (`landlord_bps` = 0).
   - `split` — deductions are real but partial; `landlord_bps`
     (0–10000) is the share of the whole deposit the landlord fairly
     keeps.
   The prompt treats everything inside the evidence tags as **data only**
   (prompt-injection guard) and forces `landlord_bps` for `release` and
   `refund`, so the verdict and the share cannot contradict each other.
3. **Validator** (`validate`): independently re-runs `evaluate()` and
   requires an exact verdict match, confidence within 20 points, and (for
   splits) `landlord_bps` within 500. Otherwise it disagrees and
   consensus rotates to a new leader.
4. **Leader errors** use the canonical taxonomy: `[EXPECTED]` business
   errors and `[EXTERNAL]` 4xx errors must match exactly to agree,
   `[TRANSIENT]` network/5xx errors agree with each other, `[LLM_ERROR]`
   (unparsable or invalid LLM output) always disagrees. Implemented via
   `gl.vm.run_nondet_unsafe(evaluate, validate)` with
   `_handle_leader_error`.

`evaluate` cannot read storage (nondet blocks can't), so all evidence is
copied from the stored record into memory first. The LLM reply is parsed
defensively (`_parse_llm_json` handles a dict return, a JSON string,
markdown fences, and single quotes) and numeric fields are coerced and
clamped.

### Methods

| Method | Type | Who | Effect |
| --- | --- | --- | --- |
| `create_lease(landlord, title, description, checkout_days, dispute_days)` | write, payable | tenant (sender) | Locks `gl.message.value` GEN as deposit; returns lease id |
| `claim_checkout(lease_id, claim_amount, evidence, evidence_url)` | write | landlord | `FUNDED → CHECKOUT_CLAIMED`; claim must be 1–deposit, evidence required, URL must be http(s) |
| `confirm_checkout(lease_id)` | write | tenant | `CHECKOUT_CLAIMED → CONFIRMED`, landlord keeps claim, remainder to tenant |
| `open_dispute(lease_id, claim, evidence)` | write | tenant | `CHECKOUT_CLAIMED → DISPUTED`; one dispute only |
| `adjudicate(lease_id)` | write | anyone | `DISPUTED → ADJUDICATED` by consensus; pays out |
| `claim_timeout_refund(lease_id)` | write | tenant | `FUNDED → REFUNDED` after the checkout deadline |
| `claim_checkout_after_window(lease_id)` | write | landlord | `CHECKOUT_CLAIMED → CONFIRMED` after the dispute deadline |
| `get_lease(lease_id)` | view | anyone | JSON record |
| `total_leases()` | view | anyone | Number of leases created |

Addresses are normalised to checksummed-hex strings at the boundary
(`_addr_str`): method arguments arrive through calldata decoding as raw
20-byte values, while `gl.message` addresses arrive as `Address` objects
— comparing them without normalising silently breaks every role check.

## Files

```
contracts/rental_deposit.py  The contract (GenLayer, Python)
tests/direct/
  conftest.py                Compatibility shims for genlayer-test 0.30.1
                             vs the v0.6 std layout (see below)
  test_rental_deposit.py     38 direct-mode tests (mocked web + LLM)
pyproject.toml               Pytest config (testpaths = tests/direct)
requirements.txt             genlayer-test, genvm-linter, pytest
gltest.config.yaml           gltest defaults (localnet, direct mode only)
LICENSE                      MIT
TEST_RESULTS.txt             Verbatim output of the last full test run
```

## Running the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/direct -v < /dev/null
```

Notes:

- Run pytest with stdin from `/dev/null`: the GenLayer std reads the
  message context from fd 0 at contract-import time, and a bare `pytest`
  run inherits a pipe that blocks forever on the first import.
- First run downloads the GenVM runner (~300 MB) into
  `~/.cache/gltest-direct/`; later runs take ~40 s. (GitHub may rate
  limit the version check — the runner falls back to the local cache.)
- Lint the contract with:

```bash
.venv/bin/genvm-lint check contracts/rental_deposit.py
```

### Test results (recorded 2026-11-27)

**38 passed, 0 failed** (`pytest tests/direct -v`, full output in
`TEST_RESULTS.txt`). Coverage by area:

- Creation & validation (11): fields/deadlines stored correctly, zero
  value, same tenant/landlord, zero-address landlord, bad title/
  description, bad windows (0 / 181 / 0 / 31 days), unknown id, lease
  counting.
- Happy path & guards (9): confirm splits the deposit by the claim;
  role checks for landlord/tenant; confirm/claim/dispute state guards;
  claim larger than the deposit rejected; empty evidence and bad
  evidence URL rejected.
- Timeouts (5): refund after the checkout deadline (and rejection
  before it); refunded lease is final; silent-tenant collection after
  the dispute window (and rejection before it / confirm after it).
- Disputes (3): dispute flow + second dispute rejected; only the tenant
  can dispute; empty claim/evidence rejected.
- Adjudication (10): release keeps the full claim / refund / split
  payouts with exact `landlord_paid` / `tenant_paid`; evidence URL
  fetched and used; dead evidence URL tolerated; unparsable LLM output
  → `[LLM_ERROR]`; adjudication requires `DISPUTED` (and cannot run
  twice); validator agrees when the LLM is stable
  (`run_validator() is True`); validator disagrees when the mocked LLM
  changes verdict; validator rejects a simulated leader error (canonical
  error handling).

What the direct-mode tests **cannot** verify (see "Known limitations"):
real value movement — `emit_transfer` is a no-op in the direct-mode mock
and payable value is not deducted from the sender — so tests assert the
contract's payout accounting, not balances. A Studio/testnet deploy is
needed to observe a real payout.

## Deploying (GenLayer CLI, StudioNet)

The contract is deployed with the GenLayer CLI (`genlayer` 0.40.1) to
the StudioNet network (chain 61999, gasless — no GEN balance needed):

```bash
export HOME=~/workspace/genlayer/cli-home
genlayer network set studionet
cat ~/workspace/genlayer/.cli-keystore-pass | genlayer deploy \
  --contract ~/workspace/genlayer/rental-deposit/contracts/rental_deposit.py
```

Verify with `genlayer schema <address>` and
`genlayer receipt <txHash> --stdout --stderr`, and check the contract
page at `https://explorer-studio.genlayer.com/address/<address>`.

## Submitting to the GenLayer Portal

Contribution type **Intelligent Contracts** (type 52, 0–500 PTS). On the
portal, submit with evidence links:

- **GitHub Repository** — the public repo containing this code.
- **GenLayer Studio Contract / GenLayer Explorer Contract** — the deployed
  testnet contract, once deployed (strong evidence that the consensus and
  payout paths work on a live network).

Notes for the submission text: state the problem (trustless settlement
of subjective deposit deductions), the consensus design (leader
adjudication under a fixed rubric + independent validator re-execution
with verdict / confidence / share tolerances), and the state machine
with deadlines. Do **not** submit a bare contract file with no deployed
instance if a deployed one is available — the guidelines favour
verifiable work.

## Known limitations / unverified API points

- **`emit_transfer`**: taken from deployed GenLayer contracts on the
  explorer, not from a current docs page (the Transaction Context page
  text was truncated when fetched). It passes `genvm-lint` and the
  direct-mode mock accepts the call, but the mock never executes the
  transfer — a testnet deploy is the real verification.
- **Clock source**: the contract uses `datetime.now(timezone.utc)` per
  the docs (Transaction Context: time is pinned to the transaction
  datetime). Some deployed contracts instead parse
  `gl.message_raw["datetime"]` with a fallback. Both should agree in
  production; the direct-mode tests confirm the `datetime.now` path
  follows `vm.warp`.
- **`run_nondet_unsafe` vs `run_nondet`**: the skill text and the
  claim-verifier reference use `run_nondet_unsafe`; an older skill text
  names `run_nondet(leader=..., validator=...)`. The contract uses
  `run_nondet_unsafe`, which matches the newest reference implementation.
- **Runner pin**: the linter notes a newer runner is available
  (`1zr6nqk597d97kg0dyxg0shhrykx5v02zjgnyrajapy4wlqvfvwh`). The pinned
  runner (`1jb45aa8…`) is the one the official write-contract guidance
  and the newest reference contracts use — and the one that deployed
  cleanly to StudioNet for the sibling Adjudicated Escrow contract.
  Review the newer runner's changelog before switching.
- **`TreeMap()` in `__init__`**: follows the claim-verifier pattern; the
  skill says `TreeMap`/`DynArray` start empty and do not need
  initialisation. Both are equivalent; the explicit call is harmless.
- **Direct-mode harness shims** (`tests/direct/conftest.py`): needed
  because genlayer-test 0.30.1 was written for the v0.3 SDK layout while
  the runner ships the v0.6 std (`genlayer.calldata` →
  `genlayer.py.calldata`, `genlayer.storage` → `genlayer.py.storage`,
  `genlayer.vm` missing). Symptoms without the shims: the contract import
  hangs on stdin / fails with `DecodingError: unexpected end of memory`,
  and storage-backed contracts crash with
  `AttributeError: __type_desc__`. If a future genlayer-test release
  fixes `sdk_compat`, the shims (and the `_sync_message` /
  `_install_nondet_shim` helpers in the test file) can be removed.
- **Validator behaviour on a live network** (leader rotation on
  disagreement, `[TRANSIENT]` agreement) is implemented per the canonical
  guidance but only the deterministic paths are exercised by the
  direct-mode tests; the transient-agreement path cannot be forced with
  mocks.

## License

MIT — see `LICENSE`.
